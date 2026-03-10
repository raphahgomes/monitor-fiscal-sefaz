from django.urls import path
from . import views

app_name = 'fiscal'

urlpatterns = [
    # Dashboard
    path('', views.FiscalDashboardView.as_view(), name='dashboard'),

    # Documentos
    path('documentos/', views.DocumentoFiscalListView.as_view(), name='documento_list'),
    path('documentos/<int:pk>/', views.DocumentoFiscalDetailView.as_view(), name='documento_detail'),

    # Ações
    path('documentos/<int:pk>/manifestacao/', views.ManifestacaoView.as_view(), name='manifestacao'),
    path('documentos/<int:pk>/download-xml/', views.DownloadXMLView.as_view(), name='download_xml'),

    # Importação
    path('importar/', views.ImportarXMLView.as_view(), name='importar_xml'),

    # Exportação
    path('exportar/excel/', views.ExportarExcelView.as_view(), name='exportar_excel'),
    path('exportar/pdf/', views.ExportarPDFView.as_view(), name='exportar_pdf'),

    # Configuração
    path('configuracao/', views.ConfiguracaoFiscalView.as_view(), name='configuracao'),

    # Ações manuais
    path('forcar-consulta/', views.ForcarConsultaView.as_view(), name='forcar_consulta'),

    # API
    path('api/totais/', views.FiscalTotaisView.as_view(), name='api_totais'),
]
