import logging
import os

from flask import g, render_template, url_for
from sqlalchemy.exc import OperationalError

from iarf_assistant.extensions import db
from iarf_assistant.models import Setting
from carlina.product_identity import PRODUCT_IDENTITY


def register_template_context_processors(app):
    @app.context_processor
    def inject_product_identity():
        return {"product": PRODUCT_IDENTITY.public_dict()}

    @app.context_processor
    def inject_ui_navigation():
        workspace_ui_contributions = []
        page_ui_contributions = []
        if hasattr(app, 'addon_manager'):
            workspace_ui_contributions = app.addon_manager.get_workspace_ui_contribution_details()
            page_ui_contributions = app.addon_manager.get_page_ui_contribution_details()
        return dict(
            workspace_ui_contributions=workspace_ui_contributions,
            page_ui_contributions=page_ui_contributions,
            automations=workspace_ui_contributions,
            addon_pages=page_ui_contributions,
        )

    @app.context_processor
    def inject_site_terms():
        """Injects site terms URL if configured."""
        try:
            site_terms_md_setting = db.session.get(Setting, 'site_terms_md')
            show_link = bool(site_terms_md_setting and site_terms_md_setting.value)
        except OperationalError:
            show_link = False

        return dict(show_terms_link=show_link)

    @app.context_processor
    def inject_ui_components():
        if getattr(g, 'rendering_ui_components', False) or getattr(g, 'rendering_helpers', False):
            return {}

        ui_components = []
        ui_component_uis_to_load = []
        ui_component_assets = {}

        def _addon_asset_url(addon_id, asset):
            asset = str(asset or '').strip().replace('\\', '/')
            if not addon_id or not asset:
                return None
            if asset.startswith(('http://', 'https://', '//', '/')):
                return asset
            asset = asset.lstrip('/')
            if asset.startswith('static/'):
                filename = asset[len('static/'):]
                try:
                    return url_for(f"{addon_id}.static", filename=filename)
                except Exception:
                    return f"/addons/{addon_id}/static/{filename}"
            return f"/addons/{addon_id}/{asset}"

        def _enrich_ui_component_assets(ui_component):
            ui_component = dict(ui_component)
            addon_id = ui_component.get('addon_id') or ui_component.get('id')
            css_assets = ui_component.get('css_assets') or ([f"static/{addon_id}.css"] if ui_component.get('css_on_chat_page') and addon_id else [])
            legacy_css_assets = ui_component.get('legacy_css_assets') or css_assets
            js_assets = ui_component.get('js_assets') or ([f"static/{addon_id}.js"] if ui_component.get('load_on_chat_page') and addon_id else [])
            ui_component['css_assets'] = css_assets
            ui_component['legacy_css_assets'] = legacy_css_assets
            ui_component['js_assets'] = js_assets
            ui_component['css_urls'] = [url for url in (_addon_asset_url(addon_id, asset) for asset in legacy_css_assets) if url]
            ui_component['js_urls'] = [url for url in (_addon_asset_url(addon_id, asset) for asset in js_assets) if url]

            aliases = ui_component.get('aliases') or []
            if isinstance(aliases, str):
                aliases = [aliases]
            keys = [
                ui_component.get('id'),
                ui_component.get('stable_id'),
                ui_component.get('component_id'),
                ui_component.get('helper_id'),
                addon_id,
                *aliases,
            ]
            keys = sorted({str(key) for key in keys if key})
            asset_record = {
                "id": ui_component.get('id'),
                "addon_id": addon_id,
                "component_id": ui_component.get('component_id') or ui_component.get('stable_id'),
                "ui_contribution_id": ui_component.get('ui_contribution_id'),
                "css_urls": ui_component['css_urls'],
                "js_urls": ui_component['js_urls'],
                "global_keys": keys,
            }
            for key in keys:
                ui_component_assets[key] = asset_record
            return ui_component

        if hasattr(app, 'addon_manager'):
            ui_components = [_enrich_ui_component_assets(ui_component) for ui_component in app.addon_manager.get_quick_access_ui_component_details()]

            g.rendering_ui_components = True
            g.rendering_helpers = True
            try:
                for ui_component in ui_components:
                    addon_id = ui_component.get('addon_id') or ui_component['id']
                    template_ref = ui_component.get('template') or f"templates/{addon_id}.html"
                    template_name = os.path.basename(template_ref)
                    if os.path.exists(os.path.join(app.root_path, 'addons', addon_id, 'templates', template_name)):
                        try:
                            ui_component_uis_to_load.append(render_template(template_name))
                        except Exception as exc:
                            logging.warning(f"Could not pre-render UI component '{ui_component['id']}': {exc}")
            finally:
                g.rendering_ui_components = False
                g.rendering_helpers = False

        return dict(
            ui_components=ui_components,
            sidebar_ui_components=ui_components,
            ui_component_uis_to_load=ui_component_uis_to_load,
            ui_component_assets=ui_component_assets,
            helpers=ui_components,
            helper_uis_to_load=ui_component_uis_to_load,
        )
