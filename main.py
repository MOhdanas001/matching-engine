
from fastapi import FastAPI

from routes import orders, book


app = FastAPI(title="RegNMS-style Matching Engine (Demo)")


app.include_router(orders.router)
app.include_router(book.router)