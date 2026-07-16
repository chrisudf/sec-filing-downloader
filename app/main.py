"""FastAPI 入口：两个 API + 静态前端。"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import edgar

app = FastAPI(title="SEC Filing Downloader", version="0.1.0")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DownloadRequest(BaseModel):
    ticker: str
    email: str
    report_types: list[str]  # "quarterly" 和/或 "annual"
    start_year: int = Field(ge=1994, le=2100)
    start_quarter: int = Field(ge=1, le=4)
    to_latest: bool = True
    end_year: int | None = Field(default=None, ge=1994, le=2100)
    end_quarter: int | None = Field(default=None, ge=1, le=4)


@app.exception_handler(edgar.EdgarError)
async def edgar_error_handler(_: Request, exc: edgar.EdgarError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.msg})


@app.get("/api/company/{ticker}")
async def company(ticker: str, email: str = "anonymous@example.com"):
    return await edgar.company_info(ticker, email)


@app.post("/api/download")
async def download(req: DownloadRequest) -> Response:
    if not EMAIL_RE.match(req.email.strip()):
        raise edgar.EdgarError(400, "请输入有效邮箱（SEC 要求提供真实联系方式）")

    forms: list[str] = []
    if "quarterly" in req.report_types:
        forms += list(edgar.QUARTER_FORMS)
    if "annual" in req.report_types:
        forms += list(edgar.ANNUAL_FORMS)
    if not forms:
        raise edgar.EdgarError(400, "请至少选择一种下载类型")

    dstart = edgar.quarter_start(req.start_year, req.start_quarter)
    if req.to_latest or req.end_year is None:
        dend = date.today()
    else:
        dend = edgar.quarter_end(req.end_year, req.end_quarter or 4)
    if dend < dstart:
        raise edgar.EdgarError(400, "结束时间早于开始时间")

    data, count = await edgar.build_zip(req.ticker, req.email.strip(), forms, dstart, dend)
    ticker = req.ticker.strip().upper()
    filename = f"{ticker}_filings_{dstart:%Y%m%d}-{dend:%Y%m%d}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-File-Count": str(count),
            "Access-Control-Expose-Headers": "X-File-Count",
        },
    )


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
