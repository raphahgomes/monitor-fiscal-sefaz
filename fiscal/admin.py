from django.contrib import admin
from .models import ConfiguracaoFiscal, DocumentoFiscalEntrada, LogConsultaSefaz


@admin.register(ConfiguracaoFiscal)
class ConfiguracaoFiscalAdmin(admin.ModelAdmin):
    list_display = ('cnpj', 'ult_nsu', 'max_nsu', 'ult_nsu_nfse', 'ambiente', 'ativo', 'ultima_consulta')
    readonly_fields = ('ult_nsu', 'max_nsu', 'ult_nsu_nfse', 'max_nsu_nfse', 'ultima_consulta', 'ultima_consulta_nfse')


@admin.register(DocumentoFiscalEntrada)
class DocumentoFiscalEntradaAdmin(admin.ModelAdmin):
    list_display = (
        'numero_nf', 'emitente_nome', 'emitente_cnpj',
        'valor_total', 'data_emissao', 'tipo_documento',
        'papel_empresa', 'status_manifestacao', 'situacao', 'origem',
    )
    list_filter = ('status_manifestacao', 'tipo_documento', 'papel_empresa', 'origem', 'situacao')
    search_fields = ('chave_acesso', 'emitente_nome', 'emitente_cnpj', 'numero_nf')
    readonly_fields = ('chave_acesso', 'nsu', 'criado_em', 'atualizado_em', 'criado_por')
    date_hierarchy = 'data_emissao'
    raw_id_fields = ['criado_por']


@admin.register(LogConsultaSefaz)
class LogConsultaSefazAdmin(admin.ModelAdmin):
    list_display = ('data_consulta', 'nsu_inicial', 'nsu_final', 'codigo_retorno', 'documentos_encontrados', 'sucesso')
    list_filter = ('sucesso',)
    readonly_fields = ('data_consulta',)
