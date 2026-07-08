from typing import Any, List, Optional

from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1200)
    aspect_ratio: str = Field(default="16:9")
    output_resolution: str = Field(default="2K")
    model: Optional[str] = None


class TokenAddRequest(BaseModel):
    token: str
    source: Optional[str] = None
    refresh_profile_id: Optional[str] = None
    refresh_profile_name: Optional[str] = None
    refresh_profile_email: Optional[str] = None
    refresh_client_id: Optional[str] = None
    account_id: Optional[str] = None
    auto_refresh: Optional[bool] = None


class TokenBatchAddRequest(BaseModel):
    tokens: List[str]


class ExportSelectionRequest(BaseModel):
    ids: Optional[List[str]] = None


class TokenCreditsBatchRefreshRequest(BaseModel):
    ids: Optional[List[str]] = None


class ConfigUpdateRequest(BaseModel):
    api_key: Optional[str] = None
    admin_username: Optional[str] = None
    admin_password: Optional[str] = None
    public_base_url: Optional[str] = None
    proxy: Optional[str] = None
    use_proxy: Optional[bool] = None
    generate_timeout: Optional[int] = None
    refresh_interval_hours: Optional[int] = None
    retry_enabled: Optional[bool] = None
    retry_max_attempts: Optional[int] = None
    retry_backoff_seconds: Optional[float] = None
    retry_on_status_codes: Optional[List[int]] = None
    retry_on_error_types: Optional[List[str]] = None
    token_rotation_strategy: Optional[str] = None
    batch_concurrency: Optional[int] = None
    generated_max_size_mb: Optional[int] = None
    generated_prune_size_mb: Optional[int] = None
    gpt_image_quality: Optional[str] = None
    adobe_register_email_provider: Optional[str] = None
    tempmail_lol_api_key: Optional[str] = None
    cloak_browser_headless: Optional[bool] = None
    cloak_browser_timeout_seconds: Optional[int] = None
    cloak_browser_binary_path: Optional[str] = None
    cloak_browser_license_key: Optional[str] = None
    cloak_browser_version: Optional[str] = None
    cloak_register_test_image: Optional[bool] = None
    cloak_register_test_model: Optional[str] = None
    cloak_register_image_timeout_seconds: Optional[int] = None


class RefreshCookieImportRequest(BaseModel):
    cookie: Any
    name: Optional[str] = None


class RefreshCookieBatchImportItem(BaseModel):
    cookie: Any
    name: Optional[str] = None


class RefreshCookieBatchImportRequest(BaseModel):
    items: List[RefreshCookieBatchImportItem]


class RefreshProfileEnabledRequest(BaseModel):
    enabled: bool


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class AdobeRegisterRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100)
    domain: Optional[str] = "trial.local"
    email_prefix: Optional[str] = "adobe_user"
    email_provider: Optional[str] = None
    tempmail_api_key: Optional[str] = None


class AdobeAccountsImportRequest(BaseModel):
    accounts: List[Any]


class PaymentCardUpsertRequest(BaseModel):
    id: Optional[str] = None
    label: Optional[str] = None
    cardholder: Optional[str] = None
    number: Optional[str] = None
    last4: Optional[str] = None
    exp_month: Optional[str] = None
    exp_year: Optional[str] = None
    cvv: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    city: Optional[str] = None
    postal_code: Optional[str] = None
    address1: Optional[str] = None
    address2: Optional[str] = None
    phone: Optional[str] = None
    source_type: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class PaymentCardsImportRequest(BaseModel):
    cards: List[Any]


class AdobeAccountUpdateRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    status: Optional[str] = None
    eligibility: Optional[str] = None
    plan: Optional[str] = None
    image_status: Optional[str] = None
    imageStatus: Optional[str] = None
    ip: Optional[str] = None
    last_action: Optional[str] = None
    lastAction: Optional[str] = None
    email_provider: Optional[str] = None
    emailProvider: Optional[str] = None
    mail_token: Optional[str] = None
    mailToken: Optional[str] = None
    mail_status: Optional[str] = None
    mailStatus: Optional[str] = None
    verification_code: Optional[str] = None
    verificationCode: Optional[str] = None
    verification_link: Optional[str] = None
    verificationLink: Optional[str] = None
    session_state_path: Optional[str] = None
    sessionStatePath: Optional[str] = None
    cookie_profile_id: Optional[str] = None
    cookieProfileId: Optional[str] = None
    token_status: Optional[str] = None
    tokenStatus: Optional[str] = None
    image_test_url: Optional[str] = None
    imageTestUrl: Optional[str] = None
    image_test_error: Optional[str] = None
    imageTestError: Optional[str] = None
    web_image_status: Optional[str] = None
    webImageStatus: Optional[str] = None
    web_image_test_url: Optional[str] = None
    webImageTestUrl: Optional[str] = None
    web_image_test_error: Optional[str] = None
    webImageTestError: Optional[str] = None
