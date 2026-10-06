import os


if os.environ.get("R02_OBSERVER_STORE_PATH"):
    import observer_host
    from src.analysis.open_intelligence import general_question_host

    general_question_host.load_question_host = observer_host.load_question_host
