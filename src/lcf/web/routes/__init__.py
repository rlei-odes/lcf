"""The routers, one per area of the application, in the order they are tried.

Routes match in registration order and a typed path parameter is validated only
after matching, so this order is behaviour rather than tidiness: the structured
builder's `/doc-types/drafts/...` is registered before `/doc-types/{key}`, which
would otherwise be free to read "drafts" as a document type key.
"""

from lcf.web.routes.evidence import router as evidence
from lcf.web.routes.factory import router as factory
from lcf.web.routes.flow import router as flow
from lcf.web.routes.installation import router as installation
from lcf.web.routes.spec_builder import router as spec_builder

ROUTERS = (installation, spec_builder, factory, evidence, flow)

__all__ = ["ROUTERS"]
