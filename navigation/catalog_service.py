"""Site-owned, versioned map and actor configuration from the frozen plugin profile."""


def apply(ctx):
    def read(request):
        if request != {}:
            raise ValueError("the catalog takes an empty request")
        return ctx.config()["settings"]["catalog"]

    ctx.provide("navigation.catalog", read)
