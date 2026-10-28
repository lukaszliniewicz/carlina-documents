(() => {
// Carlina is canonical; IARF_AI remains the same object for legacy add-ons.
window.CARLINA = window.CARLINA || window.IARF_AI || {};
window.IARF_AI = window.CARLINA;
const uiComponentRegistry = window.CARLINA.ui_components || window.CARLINA.helpers || {};
window.CARLINA.ui_components = uiComponentRegistry;
window.CARLINA.helpers = uiComponentRegistry;
window.CARLINA.ui_component_assets = window.CARLINA.ui_component_assets || {};
window.CARLINA.helper_assets = window.CARLINA.ui_component_assets;

// --- Global UI Component Loading Logic ---
const loadedUiComponents = new Map();
const loadedAssetUrls = new Set();

const uniqueValues = (values) => [...new Set(values.filter(Boolean))];

const getAssetConfig = (uiComponentId, options = {}) => {
    const registry = window.CARLINA.ui_component_assets || {};
    const keys = uniqueValues([
        uiComponentId,
        options.componentId,
        options.uiComponentId,
        options.uiContributionId,
    ]);

    for (const key of keys) {
        if (registry[key]) return registry[key];
    }
    return null;
};

const componentKeys = (uiComponentId, config = {}) => uniqueValues([
    uiComponentId,
    config.id,
    config.addon_id,
    config.component_id,
    ...(config.global_keys || []),
]);

const getRegisteredComponent = (uiComponentId, config = {}) => {
    for (const key of componentKeys(uiComponentId, config)) {
        const component = window.CARLINA.ui_components?.[key] || window.CARLINA.helpers?.[key];
        if (component) return component;
    }
    return null;
};

const rememberLoadedComponent = (uiComponentId, config, component) => {
    for (const key of componentKeys(uiComponentId, config)) {
        loadedUiComponents.set(key, component);
    }
};

const fallbackAssetUrl = (uiComponentId, extension) => `/addons/${uiComponentId}/static/${uiComponentId}.${extension}`;

const loadStylesheets = async (urls, uiComponentId, isFallback = false) => {
    for (const href of urls) {
        if (!href || loadedAssetUrls.has(href) || document.querySelector(`link[href="${href}"]`)) continue;
        if (isFallback) {
            try {
                const cssResponse = await fetch(href, { method: 'HEAD' });
                if (!cssResponse.ok) continue;
            } catch (e) {
                console.warn(`Could not check/load CSS for ${uiComponentId}:`, e);
                continue;
            }
        }

        const cssLink = document.createElement('link');
        cssLink.rel = 'stylesheet';
        cssLink.href = href;
        document.head.appendChild(cssLink);
        loadedAssetUrls.add(href);
    }
};

const loadScripts = async (urls, uiComponentId, isFallback = false) => {
    for (const src of urls) {
        if (!src || loadedAssetUrls.has(src) || document.querySelector(`script[src="${src}"]`)) {
            loadedAssetUrls.add(src);
            continue;
        }

        await new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = src;
            script.onload = () => {
                loadedAssetUrls.add(src);
                resolve();
            };
            script.onerror = () => reject(new Error(`Failed to load script for ${uiComponentId}${isFallback ? '' : `: ${src}`}`));
            document.body.appendChild(script);
        });
    }
};

window.CARLINA.loadUiComponent = async (uiComponentId, options = {}) => {
    if (loadedUiComponents.has(uiComponentId)) {
        return loadedUiComponents.get(uiComponentId);
    }
    const config = getAssetConfig(uiComponentId, options) || {};

    // Check pre-loaded
    const registeredComponent = getRegisteredComponent(uiComponentId, config);
    if (registeredComponent) {
        rememberLoadedComponent(uiComponentId, config, registeredComponent);
        return registeredComponent;
    }

    console.log(`Loading UI component: ${uiComponentId}...`);
    try {
        const hasAssetConfig = Boolean(config.id || config.addon_id || config.component_id || config.global_keys?.length);
        const cssUrls = config.css_urls?.length ? config.css_urls : (hasAssetConfig ? [] : [fallbackAssetUrl(uiComponentId, 'css')]);
        const jsUrls = config.js_urls?.length ? config.js_urls : (hasAssetConfig ? [] : [fallbackAssetUrl(uiComponentId, 'js')]);
        const usesFallbackAssets = !hasAssetConfig;

        await loadStylesheets(cssUrls, uiComponentId, usesFallbackAssets);
        await loadScripts(jsUrls, uiComponentId, usesFallbackAssets);

        const loadedComponent = getRegisteredComponent(uiComponentId, config);
        if (loadedComponent) {
            rememberLoadedComponent(uiComponentId, config, loadedComponent);
        } else {
            console.warn(`Script for ${uiComponentId} loaded but no UI component was registered.`);
        }

        return loadedUiComponents.get(uiComponentId);
    } catch (error) {
        console.error(error);
        alert(`Error loading UI component '${uiComponentId}': ${error.message}`);
        return null;
    }
};

window.CARLINA.loadHelper = window.CARLINA.loadUiComponent;

document.addEventListener('DOMContentLoaded', () => {
    // Bind click events to sidebar UI component buttons globally.
    document.querySelectorAll('.open-ui-component-btn, .open-helper-btn').forEach(button => {
        button.addEventListener('click', async (e) => {
            e.preventDefault(); // Prevent navigation if it was a link
            const uiComponentId = button.dataset.uiComponentLoaderId || button.dataset.helperId;
            const uiComponent = await window.CARLINA.loadUiComponent(uiComponentId, {
                componentId: button.dataset.uiComponentId,
                uiContributionId: button.dataset.uiContributionId,
            });
            if (uiComponent && typeof uiComponent.open === 'function') {
                await uiComponent.open();
            }
        });
    });
    
    // Check URL params for open_ui_component, preserving open_helper as a runtime alias.
    const urlParams = new URLSearchParams(window.location.search);
    const componentToOpen = urlParams.get('open_ui_component') || urlParams.get('open_helper');
    if (componentToOpen) {
        // Wait a tick for DOM to settle if needed
        setTimeout(() => {
            const btn = document.querySelector(`.open-ui-component-btn[data-ui-component-loader-id="${componentToOpen}"], .open-helper-btn[data-helper-id="${componentToOpen}"]`);
            if (btn) btn.click();
            // Clean URL
            const newUrl = window.location.pathname;
            window.history.replaceState({}, document.title, newUrl);
        }, 100);
    }
});
})();
