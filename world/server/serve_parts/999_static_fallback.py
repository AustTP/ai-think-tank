# The static catch-all MUST be the very last route registration: a Mount('/')
# full-matches every path and method, so any API route registered after it is
# silently unreachable (a POST would 405 from the mount, a GET would 404 --
# both look like the endpoint never existed). It lived at the end of
# 430_jev_model_get.py until 431_pair_service.py landed after it and its
# routes went dead; moving it to a 999-prefixed part makes "last" structural
# instead of positional luck.
#
# Static files last -- the explicit routes in every earlier part are
# registered first and always win; this is the catch-all for *.js/assets/...
# etc. (index.html itself is served by the templated route in 430, not this
# mount).
app.mount('/', StaticFiles(directory=ROOT, html=True), name='static')
