"""
Serviços de consulta de NFS-e Nacional via portal www.nfse.gov.br.

O Ambiente Nacional de NFS-e NÃO possui webservice SOAP/REST disponível
publicamente para consulta de notas recebidas pelo tomador.

A estratégia é scraping autenticado via certificado digital:
  1. Autentica no portal com o certificado PFX da empresa.
  2. Baixa a listagem de notas recebidas (até 30 dias por requisição).
  3. Extrai as chaves das NFS-e presentes na página.
  4. Baixa o XML de cada nota via /Notas/Download/NFSe/{chave}.
  5. Parseia o XML (formato SPED) e salva no banco.

Referência: https://www.nfse.gov.br/EmissorNacional
"""

import re
import logging
from datetime import datetime, timedelta, date
from decimal import Decimal

import requests
from django.utils import timezone

from .services import pfx_to_pem, strip_namespaces

logger = logging.getLogger('fiscal')

NFSE_PORTAL_BASE = 'https://www.nfse.gov.br/EmissorNacional'


# ===========================================================================
# Sessão autenticada
# ===========================================================================

def criar_sessao_portal(config) -> requests.Session:
    """
    Autentica no portal NFS-e Nacional usando o certificado PFX.
    Retorna uma Session com cookies válidos ou None se falhar.
    """
    try:
        with pfx_to_pem(config.caminho_certificado, config.senha_certificado) as cert:
            session = requests.Session()
            session.cert = cert
            session.verify = True
            session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; MonitorFiscal/1.0)'})

            r = session.get(
                f'{NFSE_PORTAL_BASE}/Certificado',
                timeout=30,
                allow_redirects=True
            )

            if 'Dashboard' in r.url or 'dashboard' in r.url or 'ASP.NET_SessionId' in session.cookies:
                # Limpar cert da sessão — cookies mantêm a autenticação
                session.cert = None
                logger.info(f'NFS-e Portal: autenticado com sucesso. URL final: {r.url}')
                return session
            else:
                logger.error(f'NFS-e Portal: autenticação falhou. URL final: {r.url}')
                return None

    except Exception as e:
        logger.error(f'NFS-e Portal: erro ao autenticar: {e}')
        return None


# ===========================================================================
# Busca de NFS-e recebidas
# ===========================================================================

def buscar_chaves_recebidas(session: requests.Session, data_inicio: date, data_fim: date) -> list:
    """Busca as chaves das NFS-e recebidas em um período (máx 30 dias)."""
    params = {
        'executar': '1',
        'busca': '',
        'datainicio': data_inicio.strftime('%d/%m/%Y'),
        'datafim': data_fim.strftime('%d/%m/%Y'),
    }
    try:
        r = session.get(
            f'{NFSE_PORTAL_BASE}/Notas/Recebidas',
            params=params,
            timeout=30
        )
        r.raise_for_status()

        chaves = re.findall(r'/Notas/Download/NFSe/([A-Za-z0-9]+)', r.text)
        chaves_unicas = list(dict.fromkeys(chaves))
        logger.info(f'NFS-e Portal: {len(chaves_unicas)} notas encontradas de '
                    f'{data_inicio:%d/%m/%Y} a {data_fim:%d/%m/%Y}')
        return chaves_unicas

    except Exception as e:
        logger.error(f'NFS-e Portal: erro ao buscar listagem: {e}')
        return []


def baixar_xml_nfse(session: requests.Session, chave: str) -> str | None:
    """Baixa o XML completo de uma NFS-e pelo identificador."""
    url = f'{NFSE_PORTAL_BASE}/Notas/Download/NFSe/{chave}'
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.error(f'NFS-e Portal: erro ao baixar XML {chave[:20]}...: {e}')
        return None


# ===========================================================================
# Parser XML NFS-e Nacional (formato SPED)
# ===========================================================================

def parsear_xml_nfse(xml_str: str, chave_portal: str) -> dict | None:
    """Parseia XML de NFS-e Nacional (formato SPED — sped.fazenda.gov.br/nfse)."""
    try:
        root = strip_namespaces(xml_str.encode('utf-8', errors='ignore'))
    except Exception as e:
        logger.error(f'NFS-e: erro ao parsear XML da chave {chave_portal[:20]}: {e}')
        return None

    infnfse = root.find('.//infNFSe')
    chave = ''
    if infnfse is not None:
        chave = infnfse.get('Id', '').strip()

    if not chave:
        chave = f'NFS{chave_portal}' if not chave_portal.startswith('NFS') else chave_portal

    cnpj_prestador = (root.findtext('.//emit/CNPJ') or
                      root.findtext('.//Cnpj') or
                      root.findtext('.//PrestadorServico/IdentificacaoPrestador/Cnpj') or '')
    nome_prestador = (root.findtext('.//emit/xNome') or
                      root.findtext('.//RazaoSocial') or
                      root.findtext('.//PrestadorServico/RazaoSocial') or '')

    valor_str = (root.findtext('.//valores/vLiq') or
                 root.findtext('.//valores/vBC') or
                 root.findtext('.//ValorServicos') or
                 root.findtext('.//Servico/Valores/ValorServicos') or '')
    valor = None
    if valor_str:
        try:
            valor = Decimal(valor_str)
        except Exception:
            pass

    numero = (root.findtext('.//nNFSe') or
              root.findtext('.//Numero') or
              root.findtext('.//InfNfse/Numero') or '')
    serie = (root.findtext('.//DPS/infDPS/serie') or
             root.findtext('.//serie') or '')

    data_str = (root.findtext('.//DPS/infDPS/dhEmi') or
                root.findtext('.//dhProc') or
                root.findtext('.//DataEmissao') or
                root.findtext('.//InfNfse/DataEmissao') or '')
    data_emissao = None
    if data_str:
        try:
            data_emissao = datetime.fromisoformat(data_str[:19])
            if data_emissao.tzinfo is None:
                data_emissao = timezone.make_aware(data_emissao)
        except Exception:
            pass

    return {
        'chave_acesso': chave,
        'cnpj_prestador': cnpj_prestador,
        'nome_prestador': nome_prestador,
        'valor': valor,
        'numero': numero,
        'serie': serie,
        'data_emissao': data_emissao,
    }


# ===========================================================================
# Função principal (chamada pela task Celery)
# ===========================================================================

def consultar_e_processar_nfse(config) -> int:
    """
    Consulta o portal NFS-e Nacional e salva as notas recebidas.
    Retorna o número de documentos novos salvos.
    """
    from .models import DocumentoFiscalEntrada

    hoje = date.today()

    if config.ultima_consulta_nfse:
        data_inicio = (config.ultima_consulta_nfse - timedelta(days=1)).date()
    else:
        data_inicio = hoje - timedelta(days=30)

    if (hoje - data_inicio).days > 30:
        data_inicio = hoje - timedelta(days=30)

    session = criar_sessao_portal(config)
    if not session:
        return 0

    try:
        chaves = buscar_chaves_recebidas(session, data_inicio, hoje)
        if not chaves:
            config.ultima_consulta_nfse = timezone.now()
            config.save(update_fields=['ultima_consulta_nfse'])
            return 0

        docs_criados = 0
        for chave_portal in chaves:
            chave_bd = f'NFS{chave_portal}' if not chave_portal.startswith('NFS') else chave_portal

            if DocumentoFiscalEntrada.objects.filter(chave_acesso=chave_bd).exists():
                continue

            xml_str = baixar_xml_nfse(session, chave_portal)
            if not xml_str:
                continue

            dados = parsear_xml_nfse(xml_str, chave_portal)
            if not dados:
                continue

            cnpj_limpo = config.cnpj.replace('.', '').replace('/', '').replace('-', '')
            papel = 'emitente' if dados['cnpj_prestador'] == cnpj_limpo else 'destinatario'

            try:
                obj, created = DocumentoFiscalEntrada.objects.update_or_create(
                    chave_acesso=dados['chave_acesso'],
                    defaults={
                        'tipo_documento': 'nfse',
                        'papel_empresa': papel,
                        'emitente_cnpj': dados['cnpj_prestador'],
                        'emitente_nome': dados['nome_prestador'],
                        'numero_nf': dados['numero'],
                        'serie': dados['serie'],
                        'data_emissao': dados['data_emissao'],
                        'valor_total': dados['valor'],
                        'xml_completo': xml_str,
                        'origem': 'sefaz',
                    }
                )
                if created:
                    docs_criados += 1
                    logger.info(
                        f'NFS-e salva: nº {dados["numero"]} de '
                        f'{dados["nome_prestador"]} — R$ {dados["valor"]}'
                    )
            except Exception as e:
                logger.error(f'NFS-e: erro ao salvar {dados["chave_acesso"][:30]}: {e}')

        config.ultima_consulta_nfse = timezone.now()
        config.save(update_fields=['ultima_consulta_nfse'])

        logger.info(f'NFS-e Nacional: {docs_criados} novos documentos')
        return docs_criados

    except Exception as e:
        logger.error(f'NFS-e Nacional: erro inesperado: {e}', exc_info=True)
        return 0
    finally:
        session.close()
