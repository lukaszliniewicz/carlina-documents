# Agent skills

Two skills for agents that work with Carlina Documents through MCP. Both use the
`SKILL.md` format read by Codex, Claude Code and compatible clients.

| Skill | What it covers |
|---|---|
| [`carlina-document-workflows`](codex/skills/carlina-document-workflows/SKILL.md) | How to operate Carlina: extraction, review, passive execution, recovery, translation runs, glossaries and exports. |
| [`translation`](codex/skills/translation/SKILL.md) | How to translate, revise and review well. It needs no Carlina tools and works on its own; its `references/carlina.md` maps it onto Translator. |

The first tells an agent which tool to call and when. The second is about the text. An
agent translating a book through Carlina should load both.

## Install

Link or copy each skill folder into your client's skills directory, keeping the folder
name. From the root of a checkout:

```bash
ln -s "$PWD/integrations/codex/skills/carlina-document-workflows" ~/.codex/skills/carlina-document-workflows
```

```bash
ln -s "$PWD/integrations/codex/skills/translation" ~/.codex/skills/translation
```

Use `~/.claude/skills/` for Claude Code. The operating skill describes the backend at the
same revision, so a link keeps the two in step; if you copy instead, update the copy
together with the application.

## Where the translation skill comes from

Its rules were extracted from translation textbooks, professional standards, localisation
and subtitling style guides, and studies of machine translation, and kept only where they
were specific enough to act on. `codex/skills/translation/references/sources.md` lists the
works and says which reference file draws on each.
