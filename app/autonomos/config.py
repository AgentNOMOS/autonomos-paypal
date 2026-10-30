"""Configuration from environment variables only. Secrets are never logged, rendered, or sent to a model.

AUTONOMOS_PAYMENT_PROVIDER  mock (default) | paypal_sandbox
PAYPAL_CLIENT_ID            sandbox REST app client id            (paypal_sandbox only)
PAYPAL_CLIENT_SECRET        sandbox REST app secret               (paypal_sandbox only; server-side only)
PAYPAL_MERCHANT_ID          expected payee merchant id (sandbox business account) - optional but recommended
PAYPAL_WEBHOOK_ID           webhook id from the sandbox app       (needed only for webhook verification)
AUTONOMOS_PUBLIC_BASE_URL   where PayPal returns the buyer        (default http://127.0.0.1:8765)
AUTONOMOS_AI_PROVIDER       fixture (default) | claude_cli | anthropic_api
ANTHROPIC_API_KEY           only for anthropic_api
AUTONOMOS_DATA_DIR          state directory (default ./var)
AUTONOMOS_SANDBOX_OWNER_GO  must equal "capture-at-most-1-order" before `serve` runs with paypal_sandbox (R1b gate)
AUTONOMOS_SANDBOX_MAX_ORDERS / AUTONOMOS_SANDBOX_MAX_CAPTURE_REQUESTS   hard caps per data dir (default 1 / 2)
"""
import os
from dataclasses import dataclass, field

SANDBOX_API_BASE = "https://api-m.sandbox.paypal.com"
LIVE_HOSTS = ("api-m.paypal.com", "api.paypal.com")     # refused unconditionally
SANDBOX_GO_TOKEN = "capture-at-most-1-order"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    payment_provider: str = "mock"
    paypal_client_id: str = ""
    paypal_client_secret: str = field(default="", repr=False)
    paypal_merchant_id: str = ""
    paypal_webhook_id: str = ""
    paypal_api_base: str = SANDBOX_API_BASE
    public_base_url: str = "http://127.0.0.1:8765"
    ai_provider: str = "fixture"
    anthropic_api_key: str = field(default="", repr=False)
    data_dir: str = "var"
    price_value: str = "1.00"
    price_currency: str = "USD"
    sandbox_owner_go: str = ""
    sandbox_max_orders: int = 1
    sandbox_max_capture_requests: int = 2

    @classmethod
    def from_env(cls, **overrides):
        e = os.environ
        c = cls(payment_provider=e.get("AUTONOMOS_PAYMENT_PROVIDER", "mock"),
                paypal_client_id=e.get("PAYPAL_CLIENT_ID", ""), paypal_client_secret=e.get("PAYPAL_CLIENT_SECRET", ""),
                paypal_merchant_id=e.get("PAYPAL_MERCHANT_ID", ""), paypal_webhook_id=e.get("PAYPAL_WEBHOOK_ID", ""),
                public_base_url=e.get("AUTONOMOS_PUBLIC_BASE_URL", "http://127.0.0.1:8765"),
                ai_provider=e.get("AUTONOMOS_AI_PROVIDER", "fixture"),
                anthropic_api_key=e.get("ANTHROPIC_API_KEY", ""), data_dir=e.get("AUTONOMOS_DATA_DIR", "var"),
                sandbox_owner_go=e.get("AUTONOMOS_SANDBOX_OWNER_GO", ""),
                sandbox_max_orders=int(e.get("AUTONOMOS_SANDBOX_MAX_ORDERS", "1")),
                sandbox_max_capture_requests=int(e.get("AUTONOMOS_SANDBOX_MAX_CAPTURE_REQUESTS", "2")))
        if overrides:
            c = cls(**{**c.__dict__, **overrides})
        c.validate()
        return c

    def validate(self):
        if self.payment_provider not in ("mock", "paypal_sandbox"):
            raise ConfigError(f"unknown payment provider {self.payment_provider!r}")
        if any(h in self.paypal_api_base for h in LIVE_HOSTS) or self.paypal_api_base != SANDBOX_API_BASE:
            raise ConfigError("only the PayPal SANDBOX API base is permitted in this build")
        if self.payment_provider == "paypal_sandbox" and not (self.paypal_client_id and self.paypal_client_secret):
            raise ConfigError("paypal_sandbox needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox app)")
        if self.payment_provider == "paypal_sandbox" and not self.paypal_merchant_id:
            raise ConfigError("paypal_sandbox needs PAYPAL_MERCHANT_ID: captures are only accepted for this payee")
        if self.ai_provider not in ("fixture", "claude_cli", "anthropic_api"):
            raise ConfigError(f"unknown AI provider {self.ai_provider!r}")

    def public_view(self):
        """Safe to display: no secrets, only whether they are set."""
        return {"payment_provider": self.payment_provider, "paypal_api_base": self.paypal_api_base,
                "paypal_client_id_set": bool(self.paypal_client_id), "paypal_secret_set": bool(self.paypal_client_secret),
                "paypal_merchant_id": self.paypal_merchant_id or None, "paypal_webhook_id_set": bool(self.paypal_webhook_id),
                "ai_provider": self.ai_provider, "anthropic_api_key_set": bool(self.anthropic_api_key),
                "price": f"{self.price_value} {self.price_currency}",
                "sandbox_caps": {"orders": self.sandbox_max_orders, "capture_requests": self.sandbox_max_capture_requests},
                "sandbox_owner_go": self.sandbox_owner_go == SANDBOX_GO_TOKEN}
