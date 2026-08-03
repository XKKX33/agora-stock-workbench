from pydantic import BaseModel


class Availability(BaseModel):
    availability: str
    reason: str
