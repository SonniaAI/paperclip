from mangum import Mangum

from app.main import app

# The same FastAPI app can run in a container or behind the managed Lambda URL.
handler = Mangum(app, lifespan="off")
