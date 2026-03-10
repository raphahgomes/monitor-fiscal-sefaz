"""
Módulo de Criptografia para Dados Sensíveis

Implementa criptografia AES-256 (via Fernet) para campos sensíveis como
senhas de certificados digitais.

Conformidade:
- LGPD (Lei 13.709/2018) — Art. 46 (medidas de segurança)
- ISO 27001:2022 — Controle 8.24 (uso de criptografia)

Uso:
    from core.encryption import EncryptedCharField

    class Config(models.Model):
        senha = EncryptedCharField(max_length=200, verbose_name="Senha")

Configuração:
    Defina ENCRYPTION_KEY no settings.py ou variável de ambiente.
    Para gerar: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import base64
import logging
from typing import Optional

from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

_fernet = None


def get_fernet():
    """Retorna instância Fernet configurada com a chave do settings."""
    global _fernet

    if _fernet is not None:
        return _fernet

    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning(
            "Cryptography não instalado. Campos criptografados funcionarão em modo texto."
        )
        return None

    key = getattr(settings, 'ENCRYPTION_KEY', None)

    if not key:
        import os
        key = os.environ.get('ENCRYPTION_KEY')

    if not key:
        logger.warning(
            "ENCRYPTION_KEY não configurada. Usando chave derivada do SECRET_KEY. "
            "Para produção, defina ENCRYPTION_KEY explicitamente."
        )
        secret = settings.SECRET_KEY
        key = base64.urlsafe_b64encode(secret[:32].ljust(32, '0').encode())

    if isinstance(key, str):
        key = key.encode()

    try:
        _fernet = Fernet(key)
    except Exception as e:
        logger.error(f"Erro ao inicializar Fernet: {e}")
        return None

    return _fernet


def encrypt_value(value: str) -> str:
    """Criptografa um valor string com AES-256."""
    if not value:
        return value

    fernet = get_fernet()
    if not fernet:
        return value

    try:
        encrypted = fernet.encrypt(value.encode())
        return encrypted.decode()
    except Exception as e:
        logger.error(f"Erro ao criptografar: {e}")
        return value


def decrypt_value(value: str) -> str:
    """Descriptografa um valor AES-256."""
    if not value:
        return value

    fernet = get_fernet()
    if not fernet:
        return value

    try:
        decrypted = fernet.decrypt(value.encode())
        return decrypted.decode()
    except Exception as e:
        logger.debug(f"Valor não criptografado ou erro: {e}")
        return value


class EncryptedCharField(models.CharField):
    """
    Campo CharField criptografado com AES-256 (Fernet).

    Armazena dados criptografados no banco e descriptografa ao ler.
    Compatível com migrações — valores não criptografados são lidos normalmente.

    Atenção: Campos criptografados NÃO podem ser usados em filtros/queries diretas.
    """

    description = "Campo de texto criptografado (AES-256)"

    def __init__(self, *args, **kwargs):
        original_max = kwargs.get('max_length', 255)
        kwargs['max_length'] = max(500, original_max * 2 + 100)
        super().__init__(*args, **kwargs)
        self._original_max_length = original_max

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if hasattr(self, '_original_max_length'):
            kwargs['max_length'] = self._original_max_length
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        return decrypt_value(value)

    def get_prep_value(self, value):
        if value is None:
            return value
        if hasattr(self, '_original_max_length') and len(str(value)) > self._original_max_length:
            raise ValueError(
                f"Valor excede tamanho máximo de {self._original_max_length} caracteres"
            )
        return encrypt_value(str(value))

    def to_python(self, value):
        if value is None:
            return value
        if isinstance(value, str):
            return decrypt_value(value)
        return value
