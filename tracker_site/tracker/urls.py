# tracker/urls.py
from django.urls import path
from .views import tx_search, last10_from_tx , download_tx_pdf_plain

urlpatterns = [
    path("", tx_search, name="tx_search"),
    path("search/", tx_search, name="tx_search"),
    path("last10/", last10_from_tx, name="last10_from_tx"),   # <-- new page
    path("search/last10/",last10_from_tx, name="last10_from_tx"),
    path("download_tx_pdf_plain/",download_tx_pdf_plain, name="download_tx_pdf_plain"),
    path("last10/", last10_from_tx, name="last10_from_tx"),
    path('download_pdf/', download_tx_pdf_plain, name='download_tx_pdf_plain')
]
    