import mistune


# CommonMark-compliant Markdown parser used by chat and policy pages.
md_parser = mistune.create_markdown(plugins=['table', 'url', 'strikethrough'])
