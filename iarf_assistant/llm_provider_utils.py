OPENAI_COMPATIBLE_MODEL_PREFIXES = ('lmstudio/', 'lm_studio/', 'vllm/')
OPENAI_COMPATIBLE_PROVIDER_NAMES = {'lmstudio', 'vllm'}
OPENAI_COMPATIBLE_PROVIDER_KIND = 'openai_compatible'


def canonical_provider_name(provider_name):
    if not provider_name:
        return provider_name
    normalized = provider_name.lower()
    if normalized == 'lm_studio':
        return 'lmstudio'
    return normalized


def is_openai_compatible_provider(provider_name, provider_kind=None):
    return (
        provider_kind == OPENAI_COMPATIBLE_PROVIDER_KIND
        or canonical_provider_name(provider_name) in OPENAI_COMPATIBLE_PROVIDER_NAMES
    )


def provider_is_openai_compatible(provider):
    if provider is None:
        return False
    return is_openai_compatible_provider(
        getattr(provider, 'name', None),
        getattr(provider, 'provider_kind', None),
    )


def infer_provider_name(model, model_record_provider_name=None):
    """Infers the credential provider from a model id or DB model record."""
    if '/' in model:
        return canonical_provider_name(model.split('/')[0])

    lowered = model.lower()
    if 'gemini' in lowered:
        return 'gemini'
    if 'claude' in lowered:
        return 'anthropic'
    if 'gpt' in lowered:
        return 'openai'
    return canonical_provider_name(model_record_provider_name) if model_record_provider_name else None


def normalize_openai_compatible_base_url(provider_name, api_base, provider_kind=None):
    """Normalizes local OpenAI-compatible servers to the /v1 endpoint LiteLLM expects."""
    if not is_openai_compatible_provider(provider_name, provider_kind) or not api_base:
        return api_base

    base_url = api_base.rstrip('/')
    if base_url.endswith('/v1'):
        base_url = base_url[:-3].rstrip('/')
    return f"{base_url}/v1"


def normalize_litellm_model_id(model, provider_name=None, provider_kind=None):
    """Maps local/custom OpenAI-compatible model ids to LiteLLM's OpenAI transport."""
    lowered = model.lower()
    prefix = model.split('/', 1)[0] if '/' in model else provider_name
    if lowered.startswith(OPENAI_COMPATIBLE_MODEL_PREFIXES) or is_openai_compatible_provider(provider_name or prefix, provider_kind):
        if '/' not in model:
            return model
        return "openai/" + model.split("/", 1)[1]
    return model
