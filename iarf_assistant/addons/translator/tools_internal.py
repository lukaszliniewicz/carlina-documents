translator_tools_def = [
    {
        "type": "function",
        "function": {
            "name": "submit_glossary_terms",
            "description": "Submit a batch of translated terms to the project glossary. Use this during the terminology analysis phase.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "terms": {
                        "type": "array",
                        "maxItems": 100,
                        "description": "List of terms to add to the glossary.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_term": {
                                    "type": "string",
                                    "maxLength": 255,
                                    "description": "The term in the source language.",
                                },
                                "target_term": {
                                    "type": "string",
                                    "maxLength": 255,
                                    "description": "The chosen translation in the target language.",
                                },
                                "notes": {
                                    "type": "string",
                                    "maxLength": 10000,
                                    "description": "Context about when to use this translation.",
                                },
                                "needs_review": {
                                    "type": "boolean",
                                    "description": "Always include this field. Use true if you are unsure and need human verification; otherwise false.",
                                },
                            },
                            "required": [
                                "source_term",
                                "target_term",
                                "notes",
                                "needs_review",
                            ],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["terms"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_glossary",
            "description": "Register a single translation for an entity that demands absolute consistency. You cannot overwrite Human-defined strict terms.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "source_term": {
                        "type": "string",
                        "maxLength": 255,
                        "description": "The term in the source language.",
                    },
                    "target_term": {
                        "type": "string",
                        "maxLength": 255,
                        "description": "The chosen translation in the target language.",
                    },
                    "notes": {
                        "type": "string",
                        "maxLength": 10000,
                        "description": "Context about when to use this translation.",
                    },
                    "needs_review": {
                        "type": "boolean",
                        "description": "Always include this field. Use true if you are unsure and need human verification; otherwise false.",
                    },
                },
                "required": ["source_term", "target_term", "notes", "needs_review"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_source_document",
            "description": "Searches the untranslated source text of the current document for a specific word or phrase to see how it was used in other sentences. Excellent for concordance analysis.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "maxLength": 1000,
                        "description": "The exact word or phrase to search for.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_working_memory",
            "description": "Append an important contextual note to your persistent working memory. Use this to remember character genders, tone, acronym meanings, or plot details.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "maxLength": 4000,
                        "description": "The factual observation to remember.",
                    }
                },
                "required": ["note"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Research an obscure term with an explicitly enabled read-only project research tool. Requested plugin IDs outside the project allowlist are blocked.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "plugin_prefixes": {
                        "type": "array",
                        "maxItems": 16,
                        "items": {"type": "string", "maxLength": 128},
                        "description": "Compatibility field: only canonical saved project add-on/plugin IDs explicitly enabled for research.",
                    },
                    "prompt": {
                        "type": "string",
                        "maxLength": 4000,
                        "description": "Detailed instructions and the query for the sub-agent.",
                    },
                },
                "required": ["plugin_prefixes", "prompt"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_project_knowledge",
            "description": "Searches the project's attached knowledge base (RAG) for background information.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "maxLength": 1000,
                        "description": "The specific question or concept to search for.",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]
