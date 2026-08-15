# -*- coding: utf-8 -*-
"""
2FA（TOTP）配置

是否在注册成功后自动设置 Authenticator 2FA：
    True:  注册完成 → 拉新 OTP 邮件 → enroll TOTP → activate → 把 secret 写入 DB
    False: 跳过整个 2FA 流程，只保存 邮箱 + accessToken

关掉开关不会影响已有 2FA 账号登录：Roxy 重新登录仍会读取账号已保存的 secret 并提交 TOTP。
"""
from config.env_loader import apply_env_overrides

ENABLE_2FA = False

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'ENABLE_2FA': 'bool'})
