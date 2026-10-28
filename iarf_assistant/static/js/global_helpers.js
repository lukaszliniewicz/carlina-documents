(() => {
// Compatibility entrypoint for legacy pages that still include global_helpers.js.
window.IARF_AI = window.IARF_AI || {};

if (!window.IARF_AI.loadUiComponent) {
    const currentScript = document.currentScript;
    const src = currentScript?.src
        ? currentScript.src.replace(/global_helpers\.js$/, 'global_ui_components.js')
        : '/static/js/global_ui_components.js';
    if (currentScript && document.readyState === 'loading') {
        document.write(`<script src="${src}"><\/script>`);
    } else {
        const script = document.createElement('script');
        script.src = src;
        document.head.appendChild(script);
    }
}
})();
