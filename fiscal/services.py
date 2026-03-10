"""
Serviços de comunicação com a SEFAZ — DistribuicaoDFe e Manifestação do Destinatário.

Fluxo:
  1. Converte PFX → PEM temporário (via cryptography)
  2. Monta SOAP XML conforme layout do Ambiente Nacional
  3. Envia via requests com mTLS (certificado do cliente)
  4. Parseia retorno, descompacta GZip, salva documentos no banco
"""

import os
import re
import gzip
import base64
import hashlib
import logging
import tempfile
import contextlib
import xml.etree.ElementTree as ET
from io import BytesIO
from datetime import datetime, timedelta
from decimal import Decimal
import pytz

import requests
from cryptography.hazmat.primitives.serialization import pkcs12, Encoding, PrivateFormat, NoEncryption
from cryptography.hazmat.primitives import hashes as crypto_hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from django.utils import timezone

logger = logging.getLogger('fiscal')

# ===========================================================================
# URLs dos Web Services SEFAZ — Ambiente Nacional
# ===========================================================================
SEFAZ_URLS = {
    1: 'https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx',  # Produção
    2: 'https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx',  # Homologação
}

SEFAZ_EVENTO_URLS = {
    1: 'https://www.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx',
    2: 'https://hom1.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx',
}

# Códigos de evento de manifestação
EVENTO_CIENCIA = '210210'
EVENTO_CONFIRMACAO = '210200'
EVENTO_DESCONHECIMENTO = '210220'
EVENTO_NAO_REALIZADA = '210240'


# ===========================================================================
# Helpers: Certificado PFX → PEM
# ===========================================================================
@contextlib.contextmanager
def pfx_to_pem(caminho_pfx: str, senha: str):
    """
    Context manager que converte um certificado PFX para PEM temporário.
    O arquivo PEM é deletado automaticamente ao sair do contexto.
    """
    if not os.path.exists(caminho_pfx):
        raise FileNotFoundError(f'Certificado PFX não encontrado: {caminho_pfx}')

    with open(caminho_pfx, 'rb') as f:
        pfx_data = f.read()

    private_key, certificate, chain = pkcs12.load_key_and_certificates(
        pfx_data,
        senha.encode('utf-8') if isinstance(senha, str) else senha
    )

    if not private_key or not certificate:
        raise ValueError('Certificado PFX inválido ou sem chave privada')

    fd, temp_pem = tempfile.mkstemp(suffix='.pem')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(private_key.private_bytes(
                encoding=Encoding.PEM,
                format=PrivateFormat.PKCS8,
                encryption_algorithm=NoEncryption()
            ))
            f.write(certificate.public_bytes(Encoding.PEM))
            if chain:
                for ca in chain:
                    f.write(ca.public_bytes(Encoding.PEM))

        yield temp_pem
    finally:
        if os.path.exists(temp_pem):
            os.remove(temp_pem)


# ===========================================================================
# Helpers: XML
# ===========================================================================
def strip_namespaces(xml_bytes):
    """Remove namespaces do XML para facilitar xpath."""
    it = ET.iterparse(BytesIO(xml_bytes) if isinstance(xml_bytes, bytes) else BytesIO(xml_bytes.encode()))
    for _, el in it:
        _, _, el.tag = el.tag.rpartition('}')
    return it.root


def _assinar_evento_nfe(evento_xml_str: str, pfx_data: bytes, senha) -> str:
    """
    Assina digitalmente um XML de evento NFe com XMLDSig RSA-SHA1 + C14N.
    Recebe a string <evento>...</evento> e retorna com <Signature> inserido.
    """
    NFE_NS = 'http://www.portalfiscal.inf.br/nfe'
    DSIG_NS = 'http://www.w3.org/2000/09/xmldsig#'

    senha_bytes = senha.encode('utf-8') if isinstance(senha, str) else senha
    private_key, certificate, _ = pkcs12.load_key_and_certificates(pfx_data, senha_bytes)
    cert_b64 = base64.b64encode(certificate.public_bytes(Encoding.DER)).decode()

    ET.register_namespace('', NFE_NS)
    ET.register_namespace('ds', DSIG_NS)
    evento_root = ET.fromstring(evento_xml_str)
    inf_evento = evento_root.find(f'{{{NFE_NS}}}infEvento')
    if inf_evento is None:
        raise ValueError('Elemento <infEvento> não encontrado no XML do evento')
    inf_id = inf_evento.get('Id', '')

    inf_str = ET.tostring(inf_evento, encoding='unicode', short_empty_elements=False)
    if f'xmlns="{NFE_NS}"' not in inf_str and 'xmlns=' not in inf_str:
        inf_str = inf_str.replace('<infEvento', f'<infEvento xmlns="{NFE_NS}"', 1)

    canonical_inf = ET.canonicalize(inf_str, with_comments=False)
    digest_b64 = base64.b64encode(
        hashlib.sha1(canonical_inf.encode('utf-8')).digest()
    ).decode()

    if not inf_id:
        raise ValueError('Atributo Id de <infEvento> não encontrado')

    signed_info_xml = (
        f'<SignedInfo xmlns="{DSIG_NS}">'
        '<CanonicalizationMethod Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315">'
        '</CanonicalizationMethod>'
        '<SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1">'
        '</SignatureMethod>'
        f'<Reference URI="#{inf_id}">'
        '<Transforms>'
        '<Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature">'
        '</Transform>'
        '<Transform Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315">'
        '</Transform>'
        '</Transforms>'
        '<DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1">'
        '</DigestMethod>'
        f'<DigestValue>{digest_b64}</DigestValue>'
        '</Reference>'
        '</SignedInfo>'
    )

    canonical_si = ET.canonicalize(signed_info_xml, with_comments=False)

    sig_bytes = private_key.sign(
        canonical_si.encode('utf-8'),
        asym_padding.PKCS1v15(),
        crypto_hashes.SHA1(),
    )
    sig_b64 = base64.b64encode(sig_bytes).decode()

    si_inner = signed_info_xml.replace(f' xmlns="{DSIG_NS}"', '')
    signature_xml = (
        f'<Signature xmlns="{DSIG_NS}">'
        f'{si_inner}'
        f'<SignatureValue>{sig_b64}</SignatureValue>'
        '<KeyInfo>'
        '<X509Data>'
        f'<X509Certificate>{cert_b64}</X509Certificate>'
        '</X509Data>'
        '</KeyInfo>'
        '</Signature>'
    )

    idx = evento_xml_str.rfind('</evento>')
    if idx < 0:
        raise ValueError('Tag </evento> não encontrada no XML')
    return evento_xml_str[:idx] + signature_xml + '</evento>'


# ===========================================================================
# 1. Consulta DistribuicaoDFe (busca novos documentos)
# ===========================================================================
def montar_soap_dist_dfe(cnpj: str, uf_codigo: str, ult_nsu: str, ambiente: int) -> str:
    """Monta o envelope SOAP 1.2 para NFeDistribuicaoDFe — consulta por último NSU."""
    return f'''<?xml version="1.0" encoding="utf-8"?>
<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                 xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                 xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">
  <soap12:Body>
    <nfeDistDFeInteresse xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe">
      <nfeDadosMsg>
        <distDFeInt xmlns="http://www.portalfiscal.inf.br/nfe" versao="1.01">
          <tpAmb>{ambiente}</tpAmb>
          <cUFAutor>{uf_codigo}</cUFAutor>
          <CNPJ>{cnpj}</CNPJ>
          <distNSU>
            <ultNSU>{ult_nsu.zfill(15)}</ultNSU>
          </distNSU>
        </distDFeInt>
      </nfeDadosMsg>
    </nfeDistDFeInteresse>
  </soap12:Body>
</soap12:Envelope>'''


def montar_soap_consulta_chave(cnpj: str, uf_codigo: str, chave: str, ambiente: int) -> str:
    """Monta o envelope SOAP para consulta por chave de acesso específica."""
    return f'''<?xml version="1.0" encoding="utf-8"?>
<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                 xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                 xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">
  <soap12:Body>
    <nfeDistDFeInteresse xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe">
      <nfeDadosMsg>
        <distDFeInt xmlns="http://www.portalfiscal.inf.br/nfe" versao="1.01">
          <tpAmb>{ambiente}</tpAmb>
          <cUFAutor>{uf_codigo}</cUFAutor>
          <CNPJ>{cnpj}</CNPJ>
          <consChNFe>
            <chNFe>{chave}</chNFe>
          </consChNFe>
        </distDFeInt>
      </nfeDadosMsg>
    </nfeDistDFeInteresse>
  </soap12:Body>
</soap12:Envelope>'''


def enviar_soap_sefaz(soap_xml: str, url: str, cert_path: str, timeout: int = 60, soapaction: str = '', soap_version: int = 12):
    """Envia requisição SOAP para a SEFAZ com certificado mTLS."""
    if soap_version == 11:
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': f'"{soapaction}"',
        }
    else:
        content_type = 'application/soap+xml; charset=utf-8'
        if soapaction:
            content_type += f'; action="{soapaction}"'
        headers = {'Content-Type': content_type}
    response = requests.post(
        url,
        data=soap_xml.encode('utf-8'),
        headers=headers,
        cert=cert_path,
        timeout=timeout,
        verify=True
    )
    if not response.ok:
        logger.error(f'SEFAZ HTTP {response.status_code} ({url}): {response.text[:3000]}')
        response.raise_for_status()
    return response.content


def consultar_dfe(config):
    """
    Consulta a SEFAZ por novos documentos (DistribuicaoDFe).
    Retorna (xml_response_bytes, sucesso: bool).
    """
    from .models import LogConsultaSefaz

    url = SEFAZ_URLS.get(config.ambiente)
    soap = montar_soap_dist_dfe(config.cnpj, config.uf_codigo, config.ult_nsu, config.ambiente)
    log = LogConsultaSefaz(nsu_inicial=config.ult_nsu)

    try:
        with pfx_to_pem(config.caminho_certificado, config.senha_certificado) as cert:
            xml_response = enviar_soap_sefaz(soap, url, cert)

        log.sucesso = True
        log.save()
        return xml_response, True

    except FileNotFoundError as e:
        log.mensagem_retorno = f'Certificado não encontrado: {e}'
        log.save()
        logger.error(f'Certificado não encontrado: {e}')
        return None, False

    except requests.exceptions.RequestException as e:
        log.mensagem_retorno = f'Erro de comunicação: {e}'
        log.save()
        logger.error(f'Erro de comunicação SEFAZ: {e}')
        return None, False

    except Exception as e:
        log.mensagem_retorno = f'Erro inesperado: {e}'
        log.save()
        logger.error(f'Erro inesperado na consulta SEFAZ: {e}')
        return None, False


def processar_retorno_dfe(xml_response: bytes, config):
    """
    Parseia o retorno da SEFAZ, descompacta os documentos e salva no banco.
    Retorna quantidade de documentos processados.
    """
    from .models import DocumentoFiscalEntrada, LogConsultaSefaz

    root = strip_namespaces(xml_response)

    ret = root.find('.//retDistDFeInt')
    if ret is None:
        ret = root

    cStat = (ret.findtext('.//cStat') or '').strip()
    xMotivo = (ret.findtext('.//xMotivo') or '').strip()
    ult_nsu_ret = (ret.findtext('.//ultNSU') or '').strip()
    max_nsu_ret = (ret.findtext('.//maxNSU') or '').strip()

    log = LogConsultaSefaz.objects.filter(nsu_inicial=config.ult_nsu).order_by('-data_consulta').first()
    if log:
        log.codigo_retorno = cStat
        log.mensagem_retorno = xMotivo
        log.max_nsu = max_nsu_ret

    if cStat not in ('138',):
        logger.info(f'SEFAZ retornou cStat={cStat}: {xMotivo}')
        if log:
            log.nsu_final = ult_nsu_ret
            log.save()
        if ult_nsu_ret and cStat == '137':
            config.ult_nsu = ult_nsu_ret
            config.ultima_consulta = timezone.now()
            config.save(update_fields=['ult_nsu', 'ultima_consulta'])
        return 0

    docs_criados = 0
    lote = ret.find('.//loteDistDFeInt')
    if lote is None:
        if log:
            log.save()
        return 0

    for doc_zip in lote.findall('docZip'):
        nsu = doc_zip.get('NSU', '').strip()
        schema = doc_zip.get('schema', '').strip()

        if not doc_zip.text:
            continue

        try:
            raw_xml = gzip.decompress(base64.b64decode(doc_zip.text))
            doc_root = strip_namespaces(raw_xml)

            # Determinar tipo de documento pelo schema
            tipo_doc = 'resumo'
            if 'procNFe' in schema or 'nfeProc' in schema:
                tipo_doc = 'nfe'
            elif 'procCTe' in schema or 'cteProc' in schema:
                tipo_doc = 'cte'
            elif 'procMDFe' in schema or 'mdfeProc' in schema:
                tipo_doc = 'mdfe'
            elif 'resNFe' in schema:
                tipo_doc = 'resumo'
            elif 'resEvento' in schema or 'procEventoNFe' in schema:
                tipo_doc = 'evento'

            chave = ''
            cnpj_emi = ''
            nome_emi = ''
            valor = None
            numero_nf = ''
            serie = ''
            data_emissao = None
            nat_op = ''
            situacao = 'autorizada'

            if tipo_doc in ('nfe', 'cte', 'mdfe'):
                chave = doc_root.findtext('.//chNFe') or doc_root.findtext('.//chCTe') or doc_root.findtext('.//chMDFe') or ''
                cnpj_emi = doc_root.findtext('.//emit/CNPJ') or ''
                nome_emi = doc_root.findtext('.//emit/xNome') or ''
                valor_str = doc_root.findtext('.//vNF') or doc_root.findtext('.//vTPrest') or doc_root.findtext('.//vCarga') or ''
                numero_nf = doc_root.findtext('.//nNF') or doc_root.findtext('.//nCT') or doc_root.findtext('.//nMDF') or ''
                serie = doc_root.findtext('.//serie') or ''
                nat_op = doc_root.findtext('.//natOp') or ''
                data_str = doc_root.findtext('.//dhEmi') or doc_root.findtext('.//dhRecbto') or ''

                if valor_str:
                    try:
                        valor = Decimal(valor_str)
                    except Exception:
                        valor = None
                if data_str:
                    try:
                        data_emissao = timezone.make_aware(datetime.fromisoformat(data_str.replace('T', ' ')[:19]))
                    except Exception:
                        data_emissao = None

                cStat_prot = doc_root.findtext('.//cStat')
                if cStat_prot in ('101', '135', '151', '155'):
                    situacao = 'cancelada'

            elif tipo_doc == 'resumo':
                chave = doc_root.findtext('.//chNFe') or ''
                cnpj_emi = doc_root.findtext('.//CNPJ') or ''
                nome_emi = doc_root.findtext('.//xNome') or ''
                valor_str = doc_root.findtext('.//vNF') or ''
                data_str = doc_root.findtext('.//dhEmi') or ''
                numero_nf = doc_root.findtext('.//nNF') or ''
                serie = doc_root.findtext('.//serie') or ''
                nat_op = doc_root.findtext('.//xNome') or ''

                cSitNFe = doc_root.findtext('.//cSitNFe') or ''
                if cSitNFe == '2':
                    situacao = 'denegada'
                elif cSitNFe == '3':
                    situacao = 'cancelada'

                if valor_str:
                    try:
                        valor = Decimal(valor_str)
                    except Exception:
                        valor = None
                if data_str:
                    try:
                        data_emissao = timezone.make_aware(datetime.fromisoformat(data_str.replace('T', ' ')[:19]))
                    except Exception:
                        data_emissao = None

            elif tipo_doc == 'evento':
                chave = doc_root.findtext('.//chNFe') or ''
                if chave:
                    tp_evento = doc_root.findtext('.//tpEvento') or ''
                    if tp_evento in ('110111',):  # Cancelamento
                        DocumentoFiscalEntrada.objects.filter(
                            chave_acesso=chave
                        ).update(situacao='cancelada')
                    elif tp_evento == '110110':  # CC-e
                        x_correcao = doc_root.findtext('.//xCorrecao') or ''
                        dh_evento = doc_root.findtext('.//dhEvento') or ''
                        data_cce = None
                        if dh_evento:
                            try:
                                data_cce = datetime.fromisoformat(dh_evento.replace('T', ' ')[:19])
                            except Exception:
                                pass
                        seq = doc_root.findtext('.//nSeqEvento') or '1'
                        chave_cce = f'{chave}CCe{seq.zfill(2)}'
                        papel_cce = _herdar_papel_cce(chave, doc_root, config.cnpj)
                        DocumentoFiscalEntrada.objects.update_or_create(
                            chave_acesso=chave_cce,
                            defaults={
                                'nsu': nsu,
                                'tipo_documento': 'cce',
                                'papel_empresa': papel_cce,
                                'emitente_cnpj': doc_root.findtext('.//CNPJ') or '',
                                'emitente_nome': '',
                                'data_emissao': data_cce,
                                'chave_referencia': chave,
                                'texto_correcao': x_correcao,
                                'schema_tipo': schema,
                                'xml_completo': raw_xml.decode('utf-8', errors='ignore'),
                                'origem': 'cce',
                            }
                        )
                        docs_criados += 1
                continue

            if not chave or len(chave) < 44:
                continue

            papel = _detectar_papel_empresa(doc_root, config.cnpj)

            defaults = {
                'nsu': nsu,
                'tipo_documento': tipo_doc if tipo_doc != 'resumo' else 'nfe',
                'emitente_cnpj': cnpj_emi,
                'emitente_nome': nome_emi,
                'numero_nf': numero_nf,
                'serie': serie,
                'data_emissao': data_emissao,
                'valor_total': valor,
                'natureza_operacao': nat_op[:255] if nat_op else '',
                'situacao': situacao,
                'schema_tipo': schema,
                'origem': 'sefaz',
            }

            if tipo_doc in ('nfe', 'cte', 'mdfe'):
                defaults['papel_empresa'] = papel
                defaults['xml_completo'] = raw_xml.decode('utf-8', errors='ignore')
            else:
                existing = DocumentoFiscalEntrada.objects.filter(chave_acesso=chave).first()
                if not existing:
                    defaults['papel_empresa'] = papel
                defaults['xml_resumo'] = raw_xml.decode('utf-8', errors='ignore')

            obj, created = DocumentoFiscalEntrada.objects.update_or_create(
                chave_acesso=chave,
                defaults=defaults,
            )
            if created:
                docs_criados += 1

        except Exception as e:
            logger.error(f'Erro ao processar doc NSU {nsu}: {e}', exc_info=True)
            continue

    if ult_nsu_ret:
        config.ult_nsu = ult_nsu_ret
    if max_nsu_ret:
        config.max_nsu = max_nsu_ret
    config.ultima_consulta = timezone.now()
    config.save(update_fields=['ult_nsu', 'max_nsu', 'ultima_consulta'])

    if log:
        log.nsu_final = ult_nsu_ret
        log.documentos_encontrados = docs_criados
        log.sucesso = True
        log.save()

    logger.info(f'DistribuicaoDFe: {docs_criados} novos documentos (NSU: {config.ult_nsu})')
    return docs_criados


# ===========================================================================
# 2. Manifestação do Destinatário
# ===========================================================================
DESCRICAO_EVENTO = {
    EVENTO_CIENCIA: 'Ciencia da Operacao',
    EVENTO_CONFIRMACAO: 'Confirmacao da Operacao',
    EVENTO_DESCONHECIMENTO: 'Desconhecimento da Operacao',
    EVENTO_NAO_REALIZADA: 'Operacao nao Realizada',
}

STATUS_POR_EVENTO = {
    EVENTO_CIENCIA: 'ciencia',
    EVENTO_CONFIRMACAO: 'confirmada',
    EVENTO_DESCONHECIMENTO: 'desconhecida',
    EVENTO_NAO_REALIZADA: 'nao_realizada',
}


def montar_soap_evento_manifestacao(
    cnpj: str,
    chave_acesso: str,
    tipo_evento: str,
    sequencia: int = 1,
    justificativa: str = '',
    ambiente: int = 1,
    pfx_data: bytes = None,
    senha: str = '',
) -> str:
    """Monta e assina o envelope SOAP para o evento de manifestação do destinatário."""
    NFE_NS = 'http://www.portalfiscal.inf.br/nfe'
    orgao = '91'  # Ambiente Nacional
    ver_evento = '1.00'
    desc_evento = DESCRICAO_EVENTO.get(tipo_evento, '')

    tz_br = pytz.timezone('America/Sao_Paulo')
    dt_evento = timezone.now().astimezone(tz_br) - timedelta(minutes=2)
    dh_evento = dt_evento.strftime('%Y-%m-%dT%H:%M:%S') + dt_evento.strftime('%z')[:3] + ':' + dt_evento.strftime('%z')[3:]

    id_evento = f'ID{tipo_evento}{chave_acesso}{str(sequencia).zfill(2)}'

    det_evento_inner = f'<descEvento>{desc_evento}</descEvento>'
    if tipo_evento in (EVENTO_NAO_REALIZADA, EVENTO_DESCONHECIMENTO) and justificativa:
        det_evento_inner += f'<xJust>{justificativa[:255]}</xJust>'

    evento_xml = (
        f'<evento xmlns="{NFE_NS}" versao="{ver_evento}">'
        f'<infEvento Id="{id_evento}">'
        f'<cOrgao>{orgao}</cOrgao>'
        f'<tpAmb>{ambiente}</tpAmb>'
        f'<CNPJ>{cnpj}</CNPJ>'
        f'<chNFe>{chave_acesso}</chNFe>'
        f'<dhEvento>{dh_evento}</dhEvento>'
        f'<tpEvento>{tipo_evento}</tpEvento>'
        f'<nSeqEvento>{sequencia}</nSeqEvento>'
        f'<verEvento>{ver_evento}</verEvento>'
        f'<detEvento versao="{ver_evento}">{det_evento_inner}</detEvento>'
        '</infEvento>'
        '</evento>'
    )

    if pfx_data and senha:
        try:
            evento_xml = _assinar_evento_nfe(evento_xml, pfx_data, senha)
        except Exception as e:
            logger.error(f'Erro ao assinar evento NFe: {e}', exc_info=True)
            raise

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        ' xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">'
        '<soap12:Header/>'
        '<soap12:Body>'
        '<nfeDadosMsg xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4">'
        f'<envEvento xmlns="{NFE_NS}" versao="{ver_evento}">'
        '<idLote>1</idLote>'
        f'{evento_xml}'
        '</envEvento>'
        '</nfeDadosMsg>'
        '</soap12:Body>'
        '</soap12:Envelope>'
    )


def enviar_manifestacao(config, documento, tipo_evento: str, justificativa: str = ''):
    """
    Envia evento de manifestação para a SEFAZ e atualiza o documento.
    Retorna (sucesso: bool, mensagem: str).
    """
    url = SEFAZ_EVENTO_URLS.get(config.ambiente)

    try:
        with open(config.caminho_certificado, 'rb') as f:
            pfx_data = f.read()
    except FileNotFoundError:
        return False, f'Certificado não encontrado: {config.caminho_certificado}'

    soap = montar_soap_evento_manifestacao(
        cnpj=config.cnpj,
        chave_acesso=documento.chave_acesso,
        tipo_evento=tipo_evento,
        justificativa=justificativa,
        ambiente=config.ambiente,
        pfx_data=pfx_data,
        senha=config.senha_certificado,
    )

    try:
        soapaction = 'http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4/nfeRecepcaoEventoNF'
        with pfx_to_pem(config.caminho_certificado, config.senha_certificado) as cert:
            xml_response = enviar_soap_sefaz(soap, url, cert, soap_version=12, soapaction=soapaction)

        root = strip_namespaces(xml_response)

        inf_evento = root.find('.//infEvento')
        if inf_evento is not None:
            cStat = inf_evento.find('cStat').text if inf_evento.find('cStat') is not None else ''
            xMotivo = inf_evento.find('xMotivo').text if inf_evento.find('xMotivo') is not None else ''
        else:
            cStat = root.findtext('.//cStat') or ''
            xMotivo = root.findtext('.//xMotivo') or ''

        if cStat in ('135', '136', '573'):
            novo_status = STATUS_POR_EVENTO.get(tipo_evento, documento.status_manifestacao)
            documento.status_manifestacao = novo_status
            documento.data_manifestacao = timezone.now()
            if justificativa:
                documento.justificativa = justificativa
            documento.save(update_fields=['status_manifestacao', 'data_manifestacao', 'justificativa'])
            logger.info(f'Manifestação {tipo_evento} enviada: {cStat} - {xMotivo}')
            return True, f'{xMotivo} (cStat: {cStat})'
        else:
            logger.warning(f'Manifestação rejeitada: {cStat} - {xMotivo}')
            return False, f'{xMotivo} (cStat: {cStat})'

    except requests.HTTPError as e:
        resp = e.response
        body = resp.content if resp is not None else b''
        if body:
            try:
                err_root = strip_namespaces(body)
                cStat = err_root.findtext('.//cStat') or ''
                xMotivo = err_root.findtext('.//xMotivo') or ''
                if cStat and xMotivo:
                    return False, f'{xMotivo} (cStat: {cStat})'
            except Exception:
                pass
        status_code = resp.status_code if resp is not None else '???'
        logger.error(f'SEFAZ HTTP {status_code} ao enviar manifestação', exc_info=True)
        return False, f'SEFAZ HTTP {status_code}: verifique os logs'
    except Exception as e:
        logger.error(f'Erro ao enviar manifestação: {e}', exc_info=True)
        return False, str(e)


def download_nfe_completa(config, chave_acesso: str):
    """
    Baixa o XML completo de uma NFe via DistribuicaoDFe (consulta por chave).
    Requer que a Ciência da Operação já tenha sido registrada.
    """
    url = SEFAZ_URLS.get(config.ambiente)
    soap = montar_soap_consulta_chave(config.cnpj, config.uf_codigo, chave_acesso, config.ambiente)

    try:
        with pfx_to_pem(config.caminho_certificado, config.senha_certificado) as cert:
            xml_response = enviar_soap_sefaz(soap, url, cert)

        root = strip_namespaces(xml_response)
        cStat = root.findtext('.//cStat') or ''
        xMotivo = root.findtext('.//xMotivo') or ''

        if cStat not in ('138',):
            return None, f'{xMotivo} (cStat: {cStat})'

        lote = root.find('.//loteDistDFeInt')
        if lote:
            for doc_zip in lote.findall('docZip'):
                if doc_zip.text:
                    raw_xml = gzip.decompress(base64.b64decode(doc_zip.text))
                    return raw_xml, 'XML baixado com sucesso'

        return None, 'Nenhum documento no retorno'

    except Exception as e:
        logger.error(f'Erro ao baixar NFe {chave_acesso}: {e}', exc_info=True)
        return None, str(e)


# ===========================================================================
# 3. Auto-detecção do papel da empresa no documento
# ===========================================================================
def _herdar_papel_cce(chave_original: str, doc_root, cnpj_empresa: str) -> str:
    """
    Determina o papel_empresa para uma CC-e (Carta de Correção).
    1) Herda do documento original no banco.
    2) Fallback: analisa o modelo fiscal pela chave (pos 20-21: 55=NFe, 57=CTe).
    3) Último recurso: detecta pelo XML.
    """
    from .models import DocumentoFiscalEntrada

    if chave_original:
        try:
            doc_orig = DocumentoFiscalEntrada.objects.filter(
                chave_acesso=chave_original
            ).values_list('papel_empresa', flat=True).first()
            if doc_orig:
                return doc_orig
        except Exception:
            pass

    if chave_original and len(chave_original) >= 22:
        modelo = chave_original[20:22]
        if modelo == '57':
            return 'emitente'
        elif modelo == '55':
            return 'transportador'

    return _detectar_papel_empresa(doc_root, cnpj_empresa)


def _detectar_papel_empresa(doc_root, cnpj_empresa: str) -> str:
    """
    Detecta o papel da empresa (dest, emit, transportador, terceiro) no XML.
    Compara o CNPJ da empresa com os CNPJs presentes no documento.
    """
    cnpj = cnpj_empresa.replace('.', '').replace('/', '').replace('-', '')

    # Emitente
    cnpj_emit = (doc_root.findtext('.//emit/CNPJ') or '').strip()
    if cnpj_emit == cnpj:
        return 'emitente'

    # Destinatário
    cnpj_dest = (doc_root.findtext('.//dest/CNPJ') or '').strip()
    if cnpj_dest == cnpj:
        return 'destinatario'

    # Transportador
    cnpj_transp = (doc_root.findtext('.//transporta/CNPJ') or '').strip()
    if cnpj_transp == cnpj:
        return 'transportador'

    # Tomador de serviço (CT-e)
    cnpj_toma = (doc_root.findtext('.//toma/CNPJ') or
                 doc_root.findtext('.//toma3/CNPJ') or
                 doc_root.findtext('.//toma4/CNPJ') or '').strip()
    if cnpj_toma == cnpj:
        return 'destinatario'

    # Remetente (CT-e)
    cnpj_rem = (doc_root.findtext('.//rem/CNPJ') or '').strip()
    if cnpj_rem == cnpj:
        return 'terceiro'

    # Expedidor / Recebedor (CT-e)
    cnpj_exped = (doc_root.findtext('.//exped/CNPJ') or '').strip()
    cnpj_receb = (doc_root.findtext('.//receb/CNPJ') or '').strip()
    if cnpj_exped == cnpj or cnpj_receb == cnpj:
        return 'terceiro'

    # Último recurso
    for elem in doc_root.iter('CNPJ'):
        if (elem.text or '').strip() == cnpj:
            return 'terceiro'

    return 'destinatario'


# ===========================================================================
# 4. Parser de XML para upload manual
# ===========================================================================
def processar_xml_upload(xml_content: bytes, cnpj_empresa: str):
    """
    Parseia um XML de NFe/CTe/MDFe/NFSe/CCe carregado manualmente.
    Retorna (dict_dados, erro_string).
    """
    try:
        root = strip_namespaces(xml_content)
    except Exception as e:
        return None, f'XML inválido: {e}'

    tipo_doc = 'nfe'
    root_tag = root.tag.lower() if root.tag else ''

    if 'cte' in root_tag or root.find('.//chCTe') is not None or root.find('.//infCte') is not None:
        tipo_doc = 'cte'
    elif 'mdfe' in root_tag or root.find('.//chMDFe') is not None or root.find('.//infMDFe') is not None:
        tipo_doc = 'mdfe'
    elif 'nfse' in root_tag or root.find('.//CompNfse') is not None or root.find('.//Nfse') is not None:
        tipo_doc = 'nfse'
    elif root.find('.//xCorrecao') is not None and root.find('.//tpEvento') is not None:
        tp_ev = root.findtext('.//tpEvento') or ''
        if tp_ev == '110110':
            tipo_doc = 'cce'
    elif 'nfe' in root_tag or root.find('.//chNFe') is not None or root.find('.//infNFe') is not None:
        tipo_doc = 'nfe'

    dados = {
        'tipo_documento': tipo_doc,
        'origem': 'upload',
        'xml_completo': xml_content.decode('utf-8', errors='ignore'),
    }

    if tipo_doc == 'cce':
        dados['chave_referencia'] = root.findtext('.//chNFe') or ''
        dados['texto_correcao'] = root.findtext('.//xCorrecao') or ''
        seq = root.findtext('.//nSeqEvento') or '1'
        chave_orig = dados['chave_referencia']
        dados['chave_acesso'] = f'{chave_orig}CCe{seq.zfill(2)}' if chave_orig else ''
        dados['emitente_cnpj'] = root.findtext('.//CNPJ') or ''
        dh = root.findtext('.//dhEvento') or ''
        if dh:
            try:
                dados['data_emissao'] = timezone.make_aware(datetime.fromisoformat(dh.replace('T', ' ')[:19]))
            except Exception:
                pass
        dados['papel_empresa'] = _herdar_papel_cce(
            dados.get('chave_referencia', ''), root, cnpj_empresa
        )
    elif tipo_doc == 'nfse':
        dados['chave_acesso'] = (root.findtext('.//chaveAcesso') or
                                  root.findtext('.//InfNfse/Numero') or
                                  root.findtext('.//CodigoVerificacao') or '')
        dados['emitente_cnpj'] = (root.findtext('.//Cnpj') or
                                   root.findtext('.//PrestadorServico/IdentificacaoPrestador/Cnpj') or '')
        dados['emitente_nome'] = (root.findtext('.//RazaoSocial') or
                                   root.findtext('.//PrestadorServico/RazaoSocial') or '')
        valor_str = (root.findtext('.//ValorServicos') or
                     root.findtext('.//Servico/Valores/ValorServicos') or '')
        if valor_str:
            try:
                dados['valor_total'] = Decimal(valor_str)
            except Exception:
                pass
        dados['numero_nf'] = root.findtext('.//Numero') or root.findtext('.//InfNfse/Numero') or ''
        dh = root.findtext('.//DataEmissao') or root.findtext('.//InfNfse/DataEmissao') or ''
        if dh:
            try:
                dados['data_emissao'] = timezone.make_aware(datetime.fromisoformat(dh.replace('T', ' ')[:19]))
            except Exception:
                pass
        dados['papel_empresa'] = _detectar_papel_empresa(root, cnpj_empresa)
    else:
        # NFe, CTe, MDFe
        chave_tags = {'nfe': './/chNFe', 'cte': './/chCTe', 'mdfe': './/chMDFe'}
        dados['chave_acesso'] = root.findtext(chave_tags.get(tipo_doc, './/chNFe')) or ''
        dados['emitente_cnpj'] = root.findtext('.//emit/CNPJ') or ''
        dados['emitente_nome'] = root.findtext('.//emit/xNome') or ''
        dados['emitente_ie'] = root.findtext('.//emit/IE') or ''

        valor_tags = {'nfe': './/vNF', 'cte': './/vTPrest', 'mdfe': './/vCarga'}
        valor_str = root.findtext(valor_tags.get(tipo_doc, './/vNF')) or ''
        if valor_str:
            try:
                dados['valor_total'] = Decimal(valor_str)
            except Exception:
                pass

        nf_tags = {'nfe': './/nNF', 'cte': './/nCT', 'mdfe': './/nMDF'}
        dados['numero_nf'] = root.findtext(nf_tags.get(tipo_doc, './/nNF')) or ''
        dados['serie'] = root.findtext('.//serie') or ''
        dados['natureza_operacao'] = root.findtext('.//natOp') or ''

        data_str = root.findtext('.//dhEmi') or ''
        if data_str:
            try:
                dados['data_emissao'] = timezone.make_aware(datetime.fromisoformat(data_str.replace('T', ' ')[:19]))
            except Exception:
                pass

        dados['papel_empresa'] = _detectar_papel_empresa(root, cnpj_empresa)

    return dados, None
