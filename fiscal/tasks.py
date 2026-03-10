"""
Tarefas Celery do módulo Fiscal — Consulta periódica à SEFAZ.

Estas tasks são agendadas via Celery Beat (ver CELERY_BEAT_SCHEDULE no settings.py).
Podem também ser disparadas manualmente na interface.
"""

import logging
from celery import shared_task

logger = logging.getLogger('fiscal')


@shared_task(name='fiscal.buscar_documentos_fiscais')
def buscar_documentos_fiscais():
    """
    Consulta a SEFAZ por novos documentos emitidos contra o CNPJ.
    Executa em loop até não haver mais documentos (ultNSU >= maxNSU).
    Respeita limite de 20 consultas por execução para evitar bloqueio.
    """
    from .models import ConfiguracaoFiscal
    from .services import consultar_dfe, processar_retorno_dfe

    config = ConfiguracaoFiscal.objects.filter(ativo=True).first()
    if not config:
        logger.warning('Nenhuma configuração fiscal ativa encontrada.')
        return 'Sem configuração fiscal ativa'

    if not config.senha_certificado:
        logger.warning('Senha do certificado não configurada.')
        return 'Senha do certificado não definida'

    total_docs = 0
    consultas = 0
    max_consultas = 20

    while consultas < max_consultas:
        consultas += 1
        logger.info(f'Consulta #{consultas} — NSU atual: {config.ult_nsu}')

        xml_response, sucesso = consultar_dfe(config)
        if not sucesso or not xml_response:
            break

        qtd = processar_retorno_dfe(xml_response, config)
        total_docs += qtd

        config.refresh_from_db()

        if config.ult_nsu >= config.max_nsu:
            logger.info('Todos os documentos disponíveis foram processados.')
            break

        if qtd == 0:
            break

    resultado = (
        f'{total_docs} documentos processados em {consultas} consulta(s). '
        f'NSU final: {config.ult_nsu}'
    )
    logger.info(resultado)
    return resultado


@shared_task(name='fiscal.manifestar_ciencia_automatica')
def manifestar_ciencia_automatica():
    """
    Envia automaticamente 'Ciência da Operação' para documentos
    que ainda estão 'Sem Manifestação', permitindo o download do XML completo.
    """
    from .models import ConfiguracaoFiscal, DocumentoFiscalEntrada
    from .services import enviar_manifestacao, EVENTO_CIENCIA

    config = ConfiguracaoFiscal.objects.filter(ativo=True).first()
    if not config:
        return 'Sem configuração fiscal'

    docs_pendentes = DocumentoFiscalEntrada.objects.filter(
        status_manifestacao='sem_manifestacao',
        situacao='autorizada',
    ).order_by('criado_em')[:10]

    resultados = []
    for doc in docs_pendentes:
        sucesso, msg = enviar_manifestacao(config, doc, EVENTO_CIENCIA)
        resultados.append(f'{doc.chave_acesso[:20]}: {"OK" if sucesso else "FALHA"} — {msg}')

    return f'{len(resultados)} manifestações enviadas'


@shared_task(name='fiscal.buscar_nfse')
def buscar_nfse():
    """
    Consulta o portal NFS-e Nacional por notas recebidas.
    Usa scraping autenticado via certificado — não há SOAP disponível.
    """
    from .models import ConfiguracaoFiscal
    from .services_nfse import consultar_e_processar_nfse

    config = ConfiguracaoFiscal.objects.filter(ativo=True).first()
    if not config:
        logger.warning('Nenhuma configuração fiscal ativa para NFS-e.')
        return 'Sem configuração fiscal ativa'

    if not config.senha_certificado:
        logger.warning('Senha do certificado não configurada (NFS-e).')
        return 'Senha do certificado não definida'

    total_docs = consultar_e_processar_nfse(config)
    resultado = f'NFS-e: {total_docs} documentos novos'
    logger.info(resultado)
    return resultado
