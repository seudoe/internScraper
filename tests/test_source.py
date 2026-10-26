import pytest

from pipeline import source_for_link


@pytest.mark.parametrize("link,expected", [
    ("https://internshala.com/internship/detail/x", "internshala"),
    ("https://www.naukri.com/job-listings-x", "naukri"),
    ("https://in.indeed.com/viewjob?jk=1", "indeed"),
    ("https://unstop.com/o/abc", "unstop"),
    ("https://letsintern.in/internship/x", "letsintern"),
    ("https://github.com/org/repo", "github"),
    ("https://careers.mastercard.com/us/en/job/1", "web"),      # company site
    ("https://boards.greenhouse.io/acme/jobs/1", "web"),
    ("https://notinternshala.com/x", "web"),                     # look-alike host is not internshala
    ("mailto:hr@acme.com", "web"),
    ("", "web"),
    (None, "web"),
])
def test_source_for_link(link, expected):
    assert source_for_link(link) == expected
