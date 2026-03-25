"""Jinja2 templates — extracted to avoid circular imports between main.py and routers."""

from pathlib import Path

from fastapi.templating import Jinja2Templates
from starlette.requests import Request


templates_dir = Path(__file__).parent.parent / "templates"
_jinja = Jinja2Templates(directory=str(templates_dir))


class CompatTemplates:
    """Wrapper that handles multiple Starlette TemplateResponse calling patterns."""

    def TemplateResponse(self, *args, request=None, name=None,
                         status_code=200, **kwargs):
        # Pattern 1: TemplateResponse(request=req, name="x.html")
        if request is not None and name is not None:
            return _jinja.TemplateResponse(
                request=request, name=name, context=kwargs, status_code=status_code
            )

        # Pattern 2: TemplateResponse("x.html", {"request": req, ...})
        if len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], dict):
            ctx = dict(args[1])
            req = ctx.pop("request", None)
            return _jinja.TemplateResponse(
                request=req, name=args[0], context=ctx, status_code=status_code
            )

        # Pattern 3: TemplateResponse("x.html", request, key=val, ...)
        if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], Request):
            return _jinja.TemplateResponse(
                request=args[1], name=args[0], context=kwargs, status_code=status_code
            )

        # Pattern 4: TemplateResponse("x.html", {"request": req, ...}, status_code=N)
        if len(args) == 1 and isinstance(args[0], str) and kwargs:
            req = kwargs.pop("request", None)
            return _jinja.TemplateResponse(
                request=req, name=args[0], context=kwargs, status_code=status_code
            )

        raise ValueError(f"Invalid TemplateResponse arguments: args={args}, kwargs={kwargs.keys()}")


templates = CompatTemplates()
