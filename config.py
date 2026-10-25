class Constants:
    _data: dict[str, str] = {
        "site_name": "S.P.F. Project",
    }

    @classmethod
    def get_all_const(cls) -> dict[str, str]:
        return cls._data.copy()
