"""Database routing for NP time-series samples (customer SKU)."""

NP_TIMESERIES_MODEL_NAMES = {
    "npresourcesample",
    "portusagesample",
    "labmetricsample",
    "labmetricrollup",
    "labresourceevent",
}


class NPTimeseriesRouter:
    """Route NP metric models to the dedicated `np_timeseries` database."""

    def db_for_read(self, model, **hints):
        if model._meta.model_name in NP_TIMESERIES_MODEL_NAMES:
            return "np_timeseries"
        return None

    def db_for_write(self, model, **hints):
        if model._meta.model_name in NP_TIMESERIES_MODEL_NAMES:
            return "np_timeseries"
        return None

    def allow_relation(self, obj1, obj2, **hints):
        names = {obj1._meta.model_name, obj2._meta.model_name}
        if names & NP_TIMESERIES_MODEL_NAMES:
            return True
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == "np_timeseries":
            if app_label == "connect" and model_name in NP_TIMESERIES_MODEL_NAMES:
                return True
            return False
        if app_label == "connect" and model_name in NP_TIMESERIES_MODEL_NAMES:
            return False
        return None
