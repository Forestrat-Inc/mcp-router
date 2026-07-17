"""Structured stdout logging + a small @log_entry_exit decorator.

Same shape as aims-rule-engine so operators see consistent lines across
services when tailing kubectl logs.
"""

import functools
import logging
import time
from typing import Any, Callable


def configure_logging(service_name: str = "mcp-router") -> None:
    logging.basicConfig(
        format=f"%(asctime)s  %(levelname)-9s [{service_name}]  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )


def log_entry_exit(logger: logging.Logger) -> Callable:
    """Log function entry + exit with duration; propagate exceptions."""

    def _decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            logger.info("entry %s", fn.__name__)
            start = time.time()
            try:
                result = fn(*args, **kwargs)
                logger.info(
                    "exit  %s ok in %.1fms",
                    fn.__name__,
                    (time.time() - start) * 1000,
                )
                return result
            except Exception as exc:
                logger.warning(
                    "exit  %s FAILED in %.1fms err=%s",
                    fn.__name__,
                    (time.time() - start) * 1000,
                    type(exc).__name__,
                )
                raise

        return _wrapper

    return _decorator
