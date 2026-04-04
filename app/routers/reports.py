from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.services.reports import generate_monthly_report, generate_yearly_report

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/monthly/{year_month}")
async def monthly_report(year_month: str):
    try:
        year, month = year_month.split("-")
        year, month = int(year), int(month)
    except ValueError:
        return JSONResponse(
            status_code=400, content={"error": "Use format YYYY-MM"}
        )

    report = await generate_monthly_report(year, month)
    return {"year": year, "month": month, "report": report}


@router.get("/yearly/{year}")
async def yearly_report(year: int):
    report = await generate_yearly_report(year)
    return {"year": year, "report": report}
