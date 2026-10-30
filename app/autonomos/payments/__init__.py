from .mock import MockPayPal, ProviderError  # noqa: F401


def make_payment_provider(cfg):
    if cfg.payment_provider == "paypal_sandbox":
        from .paypal_sandbox import PayPalSandbox
        return PayPalSandbox(cfg)
    return MockPayPal(cfg.public_base_url)
