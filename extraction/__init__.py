"""DocExtract pipeline core.

Framework-agnostic building blocks for turning unstructured business documents into
validated, structured records:

    parse -> extract (LLM) -> validate (deterministic) -> enrich -> route

Nothing in this package imports Django; the ``documents`` Django app wires these
stages to models, background tasks and the review UI.
"""
