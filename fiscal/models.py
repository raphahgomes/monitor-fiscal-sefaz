"""
Modelos do Módulo Fiscal — Manifestação do Destinatário (MDE)

Monitora NFe/CTe/MDFe/NFSe emitidas contra o CNPJ da empresa via DistribuicaoDFe (SEFAZ).
Permite upload manual de XMLs e exportação de relatórios.
"""

from django.db import models
from core.encryption import EncryptedCharField


class ConfiguracaoFiscal(models.Model):
    """
    Configuração para comunicação com a SEFAZ.
    Armazena CNPJ, caminho do certificado, senha (criptografada) e último NSU.
    """
    cnpj = models.CharField(
        max_length=14,
        verbose_name='CNPJ',
        help_text='CNPJ da empresa (somente números)'
    )
    uf_codigo = models.CharField(
        max_length=2,
        default='35',
        verbose_name='Código UF',
        help_text='Código IBGE da UF (ex: 35 = SP)'
    )
    ambiente = models.IntegerField(
        choices=[(1, 'Produção'), (2, 'Homologação')],
        default=2,
        verbose_name='Ambiente SEFAZ'
    )

    # Certificado digital
    caminho_certificado = models.CharField(
        max_length=500,
        verbose_name='Caminho do Certificado PFX',
        help_text='Caminho absoluto para o arquivo .pfx do certificado digital A1'
    )
    senha_certificado = EncryptedCharField(
        max_length=200,
        blank=True,
        default='',
        verbose_name='Senha do Certificado',
        help_text='Armazenada de forma criptografada (AES-256)'
    )

    # Controle de consulta — NFe/CTe
    ult_nsu = models.CharField(
        max_length=20,
        default='000000000000000',
        verbose_name='Último NSU',
        help_text='Último Número Sequencial Único consultado na SEFAZ'
    )
    max_nsu = models.CharField(
        max_length=20,
        default='000000000000000',
        verbose_name='Max NSU',
        help_text='Maior NSU disponível na SEFAZ (retornado na última consulta)'
    )

    # Controle de consulta — NFS-e Nacional
    ult_nsu_nfse = models.CharField(
        max_length=20,
        default='000000000000000',
        verbose_name='Último NSU (NFS-e)',
    )
    max_nsu_nfse = models.CharField(
        max_length=20,
        default='000000000000000',
        verbose_name='Max NSU (NFS-e)',
    )
    ultima_consulta_nfse = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Última Consulta NFS-e'
    )

    ultima_consulta = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Última Consulta'
    )
    ativo = models.BooleanField(
        default=True,
        verbose_name='Monitoramento Ativo'
    )

    class Meta:
        verbose_name = 'Configuração Fiscal'
        verbose_name_plural = 'Configurações Fiscais'

    def __str__(self):
        return f'Config {self.cnpj} — NSU: {self.ult_nsu} ({"Prod" if self.ambiente == 1 else "Homolog"})'


class DocumentoFiscalEntrada(models.Model):
    """
    Documento fiscal (NFe, CTe, MDFe, NFSe, etc.) emitido contra o CNPJ da empresa.
    Obtido via DistribuicaoDFe (Ambiente Nacional da SEFAZ) ou importação manual de XML.
    """

    TIPO_DOCUMENTO_CHOICES = [
        ('nfe', 'NF-e'),
        ('cte', 'CT-e'),
        ('mdfe', 'MDF-e'),
        ('nfse', 'NFS-e'),
        ('nfce', 'NFC-e'),
        ('cce', 'CC-e (Carta de Correção)'),
        ('evento', 'Evento'),
        ('resumo', 'Resumo'),
    ]

    PAPEL_EMPRESA_CHOICES = [
        ('destinatario', 'Destinatário (Compra)'),
        ('transportador', 'Transportador'),
        ('emitente', 'Emitente'),
        ('terceiro', 'Terceiro / Mencionado'),
    ]

    STATUS_MANIFESTACAO_CHOICES = [
        ('sem_manifestacao', 'Sem Manifestação'),
        ('ciencia', 'Ciência da Operação'),
        ('confirmada', 'Operação Confirmada'),
        ('desconhecida', 'Desconhecimento da Operação'),
        ('nao_realizada', 'Operação não Realizada'),
    ]

    ORIGEM_CHOICES = [
        ('sefaz', 'SEFAZ (DistribuicaoDFe)'),
        ('upload', 'Upload Manual'),
        ('cce', 'Carta de Correção'),
    ]

    STATUS_BADGES = {
        'sem_manifestacao': ('bg-yellow-100 text-yellow-800', 'fas fa-clock'),
        'ciencia': ('bg-blue-100 text-blue-800', 'fas fa-eye'),
        'confirmada': ('bg-green-100 text-green-800', 'fas fa-check-circle'),
        'desconhecida': ('bg-red-100 text-red-800', 'fas fa-times-circle'),
        'nao_realizada': ('bg-gray-100 text-gray-800', 'fas fa-ban'),
    }

    TIPO_BADGES = {
        'nfe': ('bg-blue-100 text-blue-700', 'fas fa-file-invoice'),
        'cte': ('bg-purple-100 text-purple-700', 'fas fa-truck'),
        'mdfe': ('bg-orange-100 text-orange-700', 'fas fa-route'),
        'nfse': ('bg-teal-100 text-teal-700', 'fas fa-file-contract'),
        'nfce': ('bg-green-100 text-green-700', 'fas fa-receipt'),
        'cce': ('bg-yellow-100 text-yellow-700', 'fas fa-pen'),
        'evento': ('bg-gray-100 text-gray-700', 'fas fa-bell'),
        'resumo': ('bg-gray-100 text-gray-600', 'fas fa-file'),
    }

    PAPEL_BADGES = {
        'destinatario': ('bg-blue-100 text-blue-700', 'Destinatário'),
        'transportador': ('bg-purple-100 text-purple-700', 'Transportador'),
        'emitente': ('bg-green-100 text-green-700', 'Emitente'),
        'terceiro': ('bg-gray-100 text-gray-700', 'Terceiro'),
    }

    # Identificação
    chave_acesso = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        verbose_name='Chave de Acesso'
    )
    nsu = models.CharField(
        max_length=20,
        db_index=True,
        verbose_name='NSU',
        blank=True,
        default=''
    )
    tipo_documento = models.CharField(
        max_length=10,
        choices=TIPO_DOCUMENTO_CHOICES,
        default='nfe',
        verbose_name='Tipo'
    )
    papel_empresa = models.CharField(
        max_length=20,
        choices=PAPEL_EMPRESA_CHOICES,
        default='destinatario',
        db_index=True,
        verbose_name='Papel da Empresa',
        help_text='Como a empresa aparece neste documento'
    )
    origem = models.CharField(
        max_length=10,
        choices=ORIGEM_CHOICES,
        default='sefaz',
        verbose_name='Origem',
    )

    # Emitente
    emitente_cnpj = models.CharField(max_length=14, verbose_name='CNPJ Emitente')
    emitente_nome = models.CharField(max_length=300, blank=True, default='', verbose_name='Nome Emitente')
    emitente_ie = models.CharField(max_length=20, blank=True, default='', verbose_name='IE Emitente')

    # Dados do documento
    numero_nf = models.CharField(max_length=20, blank=True, default='', verbose_name='Nº NF')
    serie = models.CharField(max_length=5, blank=True, default='', verbose_name='Série')
    data_emissao = models.DateTimeField(null=True, blank=True, verbose_name='Data Emissão')
    valor_total = models.DecimalField(
        max_digits=15, decimal_places=2,
        null=True, blank=True,
        verbose_name='Valor Total (R$)'
    )
    natureza_operacao = models.CharField(
        max_length=255, blank=True, default='',
        verbose_name='Natureza da Operação'
    )
    situacao = models.CharField(
        max_length=20, blank=True, default='autorizada',
        verbose_name='Situação',
        help_text='autorizada, cancelada, denegada'
    )

    # Manifestação
    status_manifestacao = models.CharField(
        max_length=30,
        choices=STATUS_MANIFESTACAO_CHOICES,
        default='sem_manifestacao',
        db_index=True,
        verbose_name='Manifestação'
    )
    data_manifestacao = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Data da Manifestação'
    )
    justificativa = models.TextField(
        blank=True, default='',
        verbose_name='Justificativa',
        help_text='Obrigatória para Desconhecimento ou Não Realizada'
    )

    # XML
    xml_resumo = models.TextField(blank=True, default='', verbose_name='XML Resumo')
    xml_completo = models.TextField(
        blank=True, default='',
        verbose_name='XML Completo',
        help_text='Disponível após Ciência da Operação'
    )
    schema_tipo = models.CharField(
        max_length=100, blank=True, default='',
        verbose_name='Schema',
    )

    # Arquivo XML (upload manual)
    arquivo_xml = models.FileField(
        upload_to='fiscal/xml/%Y/%m/',
        null=True, blank=True,
        verbose_name='Arquivo XML',
    )

    # CC-e — chave do documento original corrigido
    chave_referencia = models.CharField(
        max_length=44, blank=True, default='',
        verbose_name='Chave de Referência',
        help_text='Chave do documento original (para CC-e)'
    )
    texto_correcao = models.TextField(
        blank=True, default='',
        verbose_name='Texto da Correção',
    )

    # Observações internas
    observacoes = models.TextField(
        blank=True, default='',
        verbose_name='Observações Internas',
    )

    # Controle
    criado_em = models.DateTimeField(auto_now_add=True, verbose_name='Importado em')
    atualizado_em = models.DateTimeField(auto_now=True, verbose_name='Atualizado em')
    criado_por = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='docs_fiscais_criados',
        verbose_name='Importado por',
    )

    class Meta:
        verbose_name = 'Documento Fiscal de Entrada'
        verbose_name_plural = 'Documentos Fiscais de Entrada'
        ordering = ['-data_emissao', '-nsu']
        permissions = [
            ('pode_manifestar', 'Pode realizar manifestação do destinatário'),
            ('pode_importar_xml', 'Pode importar XML manualmente'),
            ('pode_exportar', 'Pode exportar relatórios fiscais'),
        ]

    def __str__(self):
        return f'{self.get_tipo_documento_display()} {self.numero_nf or self.chave_acesso[:20]} — {self.emitente_nome or self.emitente_cnpj}'

    @property
    def badge_css(self):
        return self.STATUS_BADGES.get(self.status_manifestacao, ('', ''))[0]

    @property
    def badge_icon(self):
        return self.STATUS_BADGES.get(self.status_manifestacao, ('', ''))[1]

    @property
    def tipo_badge_css(self):
        return self.TIPO_BADGES.get(self.tipo_documento, ('', ''))[0]

    @property
    def tipo_badge_icon(self):
        return self.TIPO_BADGES.get(self.tipo_documento, ('', ''))[1]

    @property
    def papel_badge_css(self):
        return self.PAPEL_BADGES.get(self.papel_empresa, ('', ''))[0]

    @property
    def papel_badge_label(self):
        return self.PAPEL_BADGES.get(self.papel_empresa, ('', ''))[1]

    @property
    def cnpj_formatado(self):
        """Retorna o CNPJ formatado (XX.XXX.XXX/XXXX-XX)."""
        c = self.emitente_cnpj or ''
        if len(c) == 14:
            return f'{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}'
        return c


class LogConsultaSefaz(models.Model):
    """Registro de cada consulta realizada à SEFAZ."""
    data_consulta = models.DateTimeField(auto_now_add=True, verbose_name='Data/Hora')
    nsu_inicial = models.CharField(max_length=20, blank=True, default='', verbose_name='NSU Inicial')
    nsu_final = models.CharField(max_length=20, blank=True, default='', verbose_name='NSU Final')
    max_nsu = models.CharField(max_length=20, blank=True, default='', verbose_name='Max NSU')
    codigo_retorno = models.CharField(max_length=10, blank=True, default='', verbose_name='cStat')
    mensagem_retorno = models.TextField(blank=True, default='', verbose_name='Mensagem')
    documentos_encontrados = models.IntegerField(default=0, verbose_name='Docs Encontrados')
    sucesso = models.BooleanField(default=False, verbose_name='Sucesso')

    class Meta:
        verbose_name = 'Log de Consulta SEFAZ'
        verbose_name_plural = 'Logs de Consultas SEFAZ'
        ordering = ['-data_consulta']

    def __str__(self):
        return f'{self.data_consulta:%d/%m/%Y %H:%M} — cStat: {self.codigo_retorno} ({self.documentos_encontrados} docs)'
