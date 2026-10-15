"""Integration test for the original query: 'show me all open opportunities where amount is greater than 20000'."""
import pytest

from app.agent.crm_query import detect_deal_filter, deal_status_query
from app.agent.schemas import CRMIntentExtraction
from app.agent.service import _ALL_OPEN_DEALS_LIST_PATTERN


ORIGINAL_QUERY = "show me all open oppurtunities where amount is greater than 20000"


def test_original_query_fallback_routing():
    """Test that the original query matches the fallback routing pattern."""
    assert _ALL_OPEN_DEALS_LIST_PATTERN.search(ORIGINAL_QUERY)


def test_original_query_amount_detection():
    """Test that the amount filter is detected in the original query."""
    deal_filter = detect_deal_filter(ORIGINAL_QUERY)
    
    assert deal_filter is not None
    assert len(deal_filter.conditions) == 1
    assert deal_filter.conditions[0].property == "amount"
    assert deal_filter.conditions[0].operator == "gt"
    assert deal_filter.conditions[0].value == "20000"


def test_original_query_status_detection():
    """Test that the open status is detected in the original query."""
    status = deal_status_query(ORIGINAL_QUERY)
    assert status == "open_deals"


def test_original_query_fallback_extraction():
    """Test that fallback routing creates correct extraction for the original query."""
    # This simulates what happens in _respond_to_extraction when extraction is None
    deal_filter = detect_deal_filter(ORIGINAL_QUERY)
    
    extraction = CRMIntentExtraction(
        intent="crm_question",
        query="open_deals",
        confidence=1.0,
        deal_filter=deal_filter.model_dump() if deal_filter else None,
    )
    
    assert extraction.intent == "crm_question"
    assert extraction.query == "open_deals"
    assert extraction.deal_filter is not None
    assert extraction.deal_filter["conditions"][0]["operator"] == "gt"
    assert extraction.deal_filter["conditions"][0]["value"] == "20000"


def test_original_query_ai_extraction_with_deal_filter():
    """Test that AI extraction would also include deal filter when added."""
    # This simulates what would happen if LLM correctly extracted "open_deals" as query
    base_extraction = CRMIntentExtraction(
        intent="crm_question",
        query="open_deals",
        confidence=0.9,
    )
    
    # Then detect_deal_filter is called and added
    deal_filter = detect_deal_filter(ORIGINAL_QUERY)
    
    final_extraction = base_extraction.model_copy(
        update={"deal_filter": deal_filter.model_dump() if deal_filter else None}
    )
    
    assert final_extraction.intent == "crm_question"
    assert final_extraction.query == "open_deals"
    assert final_extraction.deal_filter is not None
    assert final_extraction.deal_filter["conditions"][0]["operator"] == "gt"
