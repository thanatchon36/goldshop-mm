import json
import logging

from goldshop.logging_setup import JsonFormatter, RedactingFilter


def _fmt(record_fn):
    logger = logging.getLogger("t-redact")
    logger.handlers.clear()
    logger.propagate = False
    out = []

    class H(logging.Handler):
        def emit(self, r):
            out.append(self.format(r))

    h = H()
    h.setFormatter(JsonFormatter())
    f = RedactingFilter()
    f.add("SUPERSECRETKEY")
    h.addFilter(f)
    logger.addHandler(h)
    record_fn(logger)
    return [json.loads(x) for x in out]


def test_json_fields_and_redaction():
    [rec] = _fmt(lambda lg: lg.warning("key=%s", "SUPERSECRETKEY", extra={"url": "x?k=SUPERSECRETKEY", "n": 3}))
    assert rec["msg"] == "key=***"
    assert rec["url"] == "x?k=***"
    assert rec["n"] == 3 and rec["level"] == "WARNING"
