# -*- coding: utf-8 -*-
"""GCash 资格查询配置。默认只允许新版账号页手动触发。"""
from config.env_loader import apply_env_overrides


GCASH_CHECK_ENABLED = False
GCASH_CHECK_COUNTRY = "PH"
GCASH_CHECK_CURRENCY = "PHP"
GCASH_CHECK_TRIAL_DAYS = 0
GCASH_CHECK_TIMEOUT = 20.0
GCASH_CHECK_MAX_ATTEMPTS = 2
GCASH_CHECK_RETRY_DELAY = 2.0
GCASH_CHECK_WORKERS = 1
GCASH_CHECK_QUEUE_LIMIT = 100


apply_env_overrides(globals(), {
    "GCASH_CHECK_ENABLED": "bool",
    "GCASH_CHECK_COUNTRY": "str",
    "GCASH_CHECK_CURRENCY": "str",
    "GCASH_CHECK_TRIAL_DAYS": "int",
    "GCASH_CHECK_TIMEOUT": "float",
    "GCASH_CHECK_MAX_ATTEMPTS": "int",
    "GCASH_CHECK_RETRY_DELAY": "float",
    "GCASH_CHECK_WORKERS": "int",
    "GCASH_CHECK_QUEUE_LIMIT": "int",
})
