# Monitor Fiscal SEFAZ

Sistema de monitoramento automatizado de documentos fiscais eletrônicos (DF-e) via integração direta com os webservices da SEFAZ e o portal NFS-e Nacional.

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![Django](https://img.shields.io/badge/Django-5.x-green?logo=django)
![Celery](https://img.shields.io/badge/Celery-5.3-green?logo=celery)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## O que é

Uma aplicação Django completa que consulta automaticamente a **Secretaria da Fazenda (SEFAZ)** para capturar todos os documentos fiscais eletrônicos emitidos contra o CNPJ de uma empresa. Também consulta o **Portal Nacional da NFS-e** para notas fiscais de serviço.

### Tipos de documentos suportados

| Tipo | Descrição |
|------|-----------|
| **NF-e** | Nota Fiscal Eletrônica |
| **CT-e** | Conhecimento de Transporte Eletrônico |
| **MDF-e** | Manifesto Eletrônico de Documentos Fiscais |
| **NFS-e** | Nota Fiscal de Serviço Eletrônica (Portal Nacional) |
| **CC-e** | Carta de Correção Eletrônica |

---

## Funcionalidades

### Consulta Automática à SEFAZ
- Integração via **SOAP/XML** com o webservice `NFeDistribuicaoDFe`
- Autenticação **mTLS** com certificado digital A1 (`.pfx`)
- Consulta incremental via **NSU** (Número Sequencial Único)
- Descompressão automática de XML (GZip/Base64)
- Parsing e classificação de NF-e, CT-e, MDF-e, CC-e e Resumos

### Consulta de NFS-e Nacional
- Autenticação no portal `nfse.gov.br` via certificado digital
- Scraping das notas recebidas com download automático dos XMLs
- Parser de XML no formato SPED

### Manifestação do Destinatário
- **Ciência da Operação** — reconhece o recebimento e libera o XML completo
- **Confirmação da Operação** — confirma que a operação ocorreu
- **Desconhecimento** — nega conhecimento da operação
- **Operação Não Realizada** — declara que a operação não aconteceu
- Assinatura digital XML (XMLDSig/RSA-SHA1) automática

### CC-e (Carta de Correção)
- Detecção automática de CC-e no fluxo de distribuição
- Vinculação da CC-e ao documento original via `chave_referencia`
- Herança do `papel_empresa` do documento corrigido

### Interface Web
- **Dashboard** com cards de resumo por status, tipo e papel da empresa
- **Listagem** com filtros por mês/ano, status, tipo, papel e busca textual
- **Detalhe** do documento com visualização do XML, dados do emitente e ações
- **Importação manual** de XMLs via upload (drag & drop)
- **Exportação** para Excel (.xlsx) e PDF (via WeasyPrint)
- **Configuração** do certificado, CNPJ e ambiente (produção/homologação)

### Segurança
- Senha do certificado armazenada com **criptografia AES-256** (Fernet)
- Certificado PFX processado em memória (arquivos temporários seguros)
- CSRF protection em todos os formulários
- Sem hardcode de credenciais — tudo via variáveis de ambiente

### Automação (Celery)
- `buscar_documentos_fiscais` — consulta periódica à SEFAZ (NFe/CTe/MDFe)
- `manifestar_ciencia_automatica` — envia Ciência para documentos pendentes
- `buscar_nfse` — consulta periódica ao portal NFS-e Nacional
- Agendamento via Celery Beat (configurável)

---

## Arquitetura

```
monitor-fiscal-sefaz/
├── config/                  # Configuração Django (settings, urls, wsgi)
│   ├── settings.py
│   ├── urls.py
│   └── celery.py            # (adicionar se usar Celery)
├── core/
│   └── encryption.py        # Campo criptografado AES-256 (Fernet)
├── fiscal/                  # App principal
│   ├── models.py            # ConfiguracaoFiscal, DocumentoFiscalEntrada, LogConsultaSefaz
│   ├── services.py          # Comunicação SOAP com SEFAZ (mTLS, XMLDSig)
│   ├── services_nfse.py     # Scraping do portal NFS-e Nacional
│   ├── tasks.py             # Tarefas Celery
│   ├── views.py             # Dashboard, CRUD, Manifestação, Export
│   ├── urls.py              # Rotas
│   ├── admin.py             # Admin Django
│   └── templates/fiscal/    # Templates Tailwind CSS
├── templates/
│   └── base.html            # Layout base (Tailwind CDN + Alpine.js)
├── certs/                   # Certificados digitais (.pfx) — NÃO COMMITADO
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Como Usar

### Pré-requisitos

- Python 3.10+
- Certificado digital A1 (`.pfx`) da empresa
- CNPJ da empresa habilitado na SEFAZ
- (Opcional) Redis para Celery

### Instalação Local

```bash
# 1. Clonar
git clone https://github.com/raphahgomes/monitor-fiscal-sefaz.git
cd monitor-fiscal-sefaz

# 2. Ambiente virtual
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 3. Instalar dependências
pip install -r requirements.txt

# 4. Configurar variáveis de ambiente
cp .env.example .env
# Editar .env com SECRET_KEY, ENCRYPTION_KEY, etc.

# 5. Gerar chave de criptografia
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Copiar o resultado para ENCRYPTION_KEY no .env

# 6. Migrations
python manage.py makemigrations fiscal
python manage.py migrate

# 7. Criar superusuário
python manage.py createsuperuser

# 8. Rodar
python manage.py runserver
```

Acesse `http://localhost:8000/fiscal/` para o dashboard.

### Com Docker

```bash
# 1. Configurar
cp .env.example .env
# Editar .env (DB_ENGINE=django.db.backends.postgresql, etc.)

# 2. Colocar certificado em certs/
cp /caminho/do/certificado.pfx certs/

# 3. Subir
docker compose up -d

# 4. Migrations + Admin
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

### Configuração Inicial

1. Acesse `/fiscal/configuracao/`
2. Preencha o **CNPJ** da empresa (só números ou formatado)
3. Informe o **caminho do certificado** (ex: `/app/certs/meu_certificado.pfx`)
4. Digite a **senha do certificado** (será criptografada com AES-256)
5. Selecione o **ambiente** (Homologação para testes, Produção para uso real)
6. Marque **Ativo** e salve
7. Clique em **Consultar SEFAZ** no dashboard para a primeira busca

---

## Fluxo Técnico

### Consulta DF-e (SEFAZ)

```
┌─────────┐     SOAP/mTLS      ┌──────────┐
│  Celery  │ ──────────────────>│  SEFAZ   │
│  Task    │     (NFeDistDFe)   │  AN/RS   │
│          │ <──────────────────│          │
└────┬─────┘   XML + NSU       └──────────┘
     │
     │  Descomprime GZip/Base64
     │  Parseia XML (NFe/CTe/MDFe/CCe)
     │  Classifica papel (dest/transp/emit)
     │  Detecta situação (autorizada/cancelada)
     │
     ▼
┌──────────────────┐
│  PostgreSQL/     │
│  SQLite          │
│  (Documentos)    │
└──────────────────┘
```

### Manifestação

```
┌──────────┐     POST form      ┌──────────┐
│  Browser │ ──────────────────>│  Django   │
│          │                    │  View     │
└──────────┘                    └────┬─────┘
                                     │
     Gera XML de evento              │
     Assina com XMLDSig (RSA-SHA1)   │
     Envia SOAP ao webservice        │
                                     ▼
                               ┌──────────┐
                               │  SEFAZ   │
                               │  (Evento)│
                               └──────────┘
```

---

## Detalhes Técnicos

### Comunicação com SEFAZ

A comunicação utiliza o webservice `NFeDistribuicaoDFe` do Ambiente Nacional (AN), hospedado na SEFAZ-RS:
- **Produção**: `https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx`
- **Homologação**: `https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx`

A autenticação é feita via **mTLS** (mutual TLS) com o certificado digital A1 da empresa. O certificado PFX é convertido para PEM em memória usando a biblioteca `cryptography`.

### Assinatura Digital (XMLDSig)

A manifestação do destinatário requer assinatura digital do XML do evento. O sistema implementa:
1. Canonicalização C14N do XML
2. Digest SHA-1 da referência
3. Assinatura RSA-SHA1 com a chave privada do certificado
4. Inserção do elemento `<Signature>` no envelope

### Criptografia da Senha

A senha do certificado é armazenada no banco de dados usando criptografia simétrica **AES-256 via Fernet** (biblioteca `cryptography`). A chave é definida na variável de ambiente `ENCRYPTION_KEY`.

### NFS-e Nacional

O portal NFS-e Nacional (`nfse.gov.br`) não disponibiliza webservice público para consulta de notas recebidas. A solução utiliza:
1. Autenticação via certificado digital no endpoint `/EmissorNacional/Certificado`
2. Listagem de notas recebidas via scraping da página `/Notas/Recebidas`
3. Download individual do XML de cada nota via `/Notas/Download/NFSe/{chave}`
4. Parsing do XML no formato SPED (SPEDFazenda/NFS-e)

---

## Stack Tecnológica

| Camada | Tecnologia |
|--------|-----------|
| Backend | Django 5.x (Python 3.12) |
| Banco de Dados | PostgreSQL 16 / SQLite |
| Task Queue | Celery 5.3 + Redis |
| Frontend | Tailwind CSS + Alpine.js |
| Comunicação SEFAZ | requests + mTLS + SOAP/XML |
| Assinatura Digital | cryptography (RSA + SHA1) |
| Criptografia | Fernet (AES-256-CBC) |
| Exportação | openpyxl (Excel) + WeasyPrint (PDF) |
| Deploy | Docker + Gunicorn |

---

## Licença

MIT License — veja [LICENSE](LICENSE) para detalhes.

---

## Autor

Desenvolvido por **Raphael Gomes** — [GitHub](https://github.com/raphahgomes)
