from django.urls import path
from .views import tx_search

urlpatterns = [
    path("", tx_search, name="tx_search"),              # Default homepage → HTML view
    path("search/", tx_search, name="tx_search"),       # Explicit HTML search endpoint
    # path("api/search/", tx_search_api, name="tx_search_api"),  # JSON API endpoint
]
