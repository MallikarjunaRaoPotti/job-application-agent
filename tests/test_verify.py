import pytest

from verify_jobs import open_state, screen

DESC = "x" * 400


def job(**kw):
    return {"company": "Acme Analytics", "company_industry": "Software Development", "description": DESC,
            "company_linkedin_url": "https://www.linkedin.com/company/acme", **kw}


@pytest.mark.parametrize("kw,status", [
    ({"company_industry": "Staffing and Recruiting"}, "excluded_staffing"),
    ({"company": "OrbiTouch HR (orbiTouch outsourcing pvt ltd)"}, "excluded_staffing"),
    ({"company": "Quess Manpower Services"}, "excluded_staffing"),
    ({"description": DESC + " We are hiring a Data Analyst for our client, a leading bank."}, "excluded_staffing"),
    ({"description": DESC + " Our client is a fast-growing fintech."}, "excluded_staffing"),
    ({"description": "Data analyst role. Apply now."}, "excluded_red_flag"),
    ({"description": DESC + " Candidates must pay a registration fee of Rs 2000."}, "excluded_red_flag"),
    ({"description": DESC + " This is an unpaid internship."}, "excluded_red_flag"),
    ({"company_linkedin_url": None}, "excluded_red_flag"),
])
def test_screen_excludes(kw, status):
    assert screen(job(**kw))[0] == status


@pytest.mark.parametrize("kw", [
    {},
    {"description": DESC + " We significantly reduce transaction costs for our clients."},   # BestEx (real)
    {"description": DESC + " We deliver analytics on behalf of our clients worldwide."},
    {"company": "Tata Consultancy Services", "company_industry": "IT Services and IT Consulting"},
    {"company": "Hrishikesh Analytics"},                     # 'hr' inside a word
])
def test_screen_keeps_direct_employers(kw):
    j = job(**kw)
    if "company_linkedin_url" not in kw:
        j.pop("company_linkedin_url")    # older rows without the field aren't penalised
    assert screen(j) is None


def test_open_state():
    assert open_state({"jobState": "CLOSED"})[0] is False
    assert open_state({"closedNotice": "No longer accepting applications"})[0] is False
    assert open_state({"jobApplicationLimitReached": True})[0] is False
    assert open_state({"jobState": None})[0] is True
