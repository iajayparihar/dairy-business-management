import re
from decimal import Decimal

from rest_framework.test import APIClient

_MONEY = re.compile(r"^-?\d+\.\d{2,3}$")


def _decimalize(value):
    if isinstance(value, str) and _MONEY.match(value):
        return Decimal(value)
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    return value


class DecimalClient(APIClient):
    """The API sends money as exact strings; these older tests compare Decimals."""

    def request(self, **kwargs):
        response = super().request(**kwargs)
        data = getattr(response, "data", None)
        if isinstance(data, (dict, list)):
            response.data = _decimalize(data)
        return response
