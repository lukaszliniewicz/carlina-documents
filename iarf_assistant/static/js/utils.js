const apiRequest = async (endpoint, method, body = null) => {
    const options = {
        method: method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (body) {
        options.body = JSON.stringify(body);
    }
    const response = await fetch(endpoint, options);
    
    if (!response.ok) {
        let errorData;
        try {
            errorData = await response.json();
        } catch(_error) {
            // If the body is not JSON or empty
            errorData = { error: `Request to ${endpoint} failed with status ${response.status}. Response was not valid JSON.` };
        }
        throw new Error(errorData.error || `Request to ${endpoint} failed with status ${response.status}.`);
    }
    // Handle cases with no response body (e.g., 204 No Content)
    if (response.status === 204) {
        return { success: true };
    }
    return await response.json();
};

/**
 * Populates a select element with LLM models grouped by provider using <optgroup>.
 * @param {HTMLSelectElement|string} selectElementOrId - The select element or its ID.
 * @param {Array} models - Array of model objects from /api/llm/models.
 * @param {Object} options - Configuration options:
 *   - {string} selectedValue - The value to pre-select.
 *   - {string} placeholder - Optional text for an empty/default first option.
 *   - {string} preferredModel - A model ID to select if no selectedValue is matched.
 *   - {Array} preferredKeywords - Array of strings/substrings to match (case-insensitive) for default selection.
 */
const populateGroupedModelSelect = (selectElementOrId, models, options = {}) => {
    const select = typeof selectElementOrId === 'string' ? document.getElementById(selectElementOrId) : selectElementOrId;
    if (!select) return;

    const {
        selectedValue = '',
        placeholder = '',
        preferredModel = '',
        preferredKeywords = []
    } = options;

    select.innerHTML = '';

    if (placeholder) {
        const placeholderOption = document.createElement('option');
        placeholderOption.value = '';
        placeholderOption.textContent = placeholder;
        select.appendChild(placeholderOption);
    }

    // Group models by provider
    const grouped = {};
    models.forEach(model => {
        const provider = model.provider || 'Other';
        if (!grouped[provider]) {
            grouped[provider] = [];
        }
        grouped[provider].push(model);
    });

    // Determine target pre-selected value
    const hasValue = (val) => models.some(m => m.id === val);
    let targetValue = selectedValue;

    if (!targetValue || !hasValue(targetValue)) {
        if (preferredModel && hasValue(preferredModel)) {
            targetValue = preferredModel;
        } else if (preferredKeywords && preferredKeywords.length > 0) {
            for (const kw of preferredKeywords) {
                const match = models.find(m => m.id.toLowerCase().includes(kw.toLowerCase()));
                if (match) {
                    targetValue = match.id;
                    break;
                }
            }
        }
    }

    let selectedSet = false;

    Object.keys(grouped).sort().forEach(provider => {
        const optgroup = document.createElement('optgroup');
        optgroup.label = provider;

        grouped[provider].forEach(model => {
            const option = document.createElement('option');
            option.value = model.id;
            option.textContent = model.name;
            optgroup.appendChild(option);

            if (model.id === targetValue) {
                option.selected = true;
                selectedSet = true;
            }
        });

        select.appendChild(optgroup);
    });

    if (targetValue && !selectedSet) {
        select.value = targetValue;
    }
};

window.populateGroupedModelSelect = populateGroupedModelSelect;

const addonReasoningSettingsCache = new Map();

const getAddonReasoningSettings = async (addonId) => {
    if (!addonReasoningSettingsCache.has(addonId)) {
        const request = apiRequest(`/api/llm/addon-settings/${encodeURIComponent(addonId)}/reasoning`, 'GET')
            .then((settings) => settings || { slots: {} })
            .catch((error) => {
                addonReasoningSettingsCache.delete(addonId);
                throw error;
            });
        addonReasoningSettingsCache.set(addonId, request);
    }
    return addonReasoningSettingsCache.get(addonId);
};

const normalizeReasoningLevels = (model) => {
    if (!model?.supports_reasoning) return [];
    const levels = Array.isArray(model.supported_reasoning_levels)
        ? model.supported_reasoning_levels
        : String(model.supported_reasoning_levels || '').split(',');
    const allowed = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh'];
    const normalized = levels.map((level) => String(level).trim().toLowerCase())
        .filter((level, index, values) => allowed.includes(level) && values.indexOf(level) === index);
    return normalized;
};

const reasoningLevelLabel = (level) => ({
    off: 'Off',
    minimal: 'Minimal',
    low: 'Low',
    medium: 'Medium',
    high: 'High',
    xhigh: 'Extra high',
}[level] || level);

/**
 * Mounts a small reasoning-level control beside an add-on's existing model
 * selector. The add-on owns the model picker; this helper only persists the
 * selected model/role's reasoning preference.
 */
const mountAddonReasoningControl = async ({ addonId, slot = 'default', modelSelect, getModelId, anchorElement, models, label = 'Reasoning effort' }) => {
    const select = typeof modelSelect === 'string' ? document.getElementById(modelSelect) : modelSelect;
    if ((!select && typeof getModelId !== 'function') || !addonId || !Array.isArray(models)) return null;

    const existing = document.querySelector(`[data-addon-reasoning-control="${addonId}:${slot}"]`);
    if (existing) return { refresh: () => {} };

    const wrapper = document.createElement('div');
    wrapper.dataset.addonReasoningControl = `${addonId}:${slot}`;
    wrapper.style.cssText = 'margin-top: 0.45rem; display: none;';

    const controlLabel = document.createElement('label');
    controlLabel.textContent = label;
    controlLabel.style.cssText = 'display: block; font-size: 0.8rem; color: #666; margin-bottom: 0.2rem;';
    const levelSelect = document.createElement('select');
    levelSelect.setAttribute('aria-label', label);
    levelSelect.style.cssText = 'width: 100%; box-sizing: border-box; font: inherit;';
    controlLabel.appendChild(levelSelect);
    wrapper.appendChild(controlLabel);

    const anchor = anchorElement || select?.closest('label') || select;
    if (!anchor) return null;
    anchor.insertAdjacentElement('afterend', wrapper);

    let settings;
    try {
        settings = await getAddonReasoningSettings(addonId);
    } catch (error) {
        console.error(`Unable to load reasoning settings for ${addonId}:`, error);
        wrapper.remove();
        return null;
    }

    const refresh = () => {
        const modelId = select ? select.value : getModelId();
        const model = models.find((candidate) => candidate.id === modelId);
        const levels = normalizeReasoningLevels(model);
        wrapper.style.display = levels.length ? 'block' : 'none';
        levelSelect.innerHTML = '';
        if (!levels.length) return;

        const defaultOption = document.createElement('option');
        defaultOption.value = 'default';
        defaultOption.textContent = 'Model default';
        levelSelect.appendChild(defaultOption);
        levels.forEach((level) => {
            const option = document.createElement('option');
            option.value = level;
            option.textContent = reasoningLevelLabel(level);
            levelSelect.appendChild(option);
        });

        const saved = settings?.slots?.[slot];
        levelSelect.value = saved?.model_id === modelId && levels.includes(saved.level)
            ? saved.level
            : 'default';
    };

    if (select) select.addEventListener('change', refresh);
    levelSelect.addEventListener('change', async () => {
        const level = levelSelect.value;
        const record = { model_id: select ? select.value : getModelId(), level };
        levelSelect.disabled = true;
        try {
            const updatedSettings = await apiRequest(
                `/api/llm/addon-settings/${encodeURIComponent(addonId)}/reasoning`,
                'PUT',
                { slots: { [slot]: record } },
            );
            settings = updatedSettings;
            addonReasoningSettingsCache.set(addonId, Promise.resolve(updatedSettings));
        } catch (error) {
            console.error(`Unable to save reasoning settings for ${addonId}:`, error);
            window.alert(`Unable to save reasoning preference: ${error.message}`);
            refresh();
        } finally {
            levelSelect.disabled = false;
        }
    });

    refresh();
    return { refresh, levelSelect };
};

window.getAddonReasoningSettings = getAddonReasoningSettings;
window.mountAddonReasoningControl = mountAddonReasoningControl;
