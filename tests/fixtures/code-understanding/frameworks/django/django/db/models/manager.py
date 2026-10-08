"""Synthetic framework identity; no database behavior is implemented."""


class Manager:
    def get_queryset(self):
        return ()
