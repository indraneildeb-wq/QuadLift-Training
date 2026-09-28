import streamlit as st
from typing import TypedDict
from langgraph.graph import StateGraph, END

# -------------------------------------------------
# HITL CONFIGURATION
# -------------------------------------------------

AUTO_APPROVE_COST_INCREASE = 5
MANAGER_APPROVAL_COST = 25

AUTO_APPROVE_CARGO_VALUE = 50000
DIRECTOR_APPROVAL_CARGO = 250000

# -------------------------------------------------
# STATE
# -------------------------------------------------

class ShipmentState(TypedDict):
    shipment_id: str
    source_port: str
    destination_port: str
    cargo_value: float
    current_cost: float
    proposed_cost: float
    cost_increase_pct: float
    approved: bool
    status: str


# -------------------------------------------------
# DISRUPTION + REROUTE PLANNER AGENT
# -------------------------------------------------

def planner(state: ShipmentState):

    increase_pct = (
        (state["proposed_cost"] - state["current_cost"])
        / state["current_cost"]
    ) * 100

    state["cost_increase_pct"] = round(increase_pct, 2)

    cargo_value = state["cargo_value"]

    # Low Risk
    if (
        increase_pct <= AUTO_APPROVE_COST_INCREASE
        and cargo_value <= AUTO_APPROVE_CARGO_VALUE
    ):
        state["status"] = "AUTO_APPROVED"

    # High Risk
    elif (
        increase_pct > MANAGER_APPROVAL_COST
        or cargo_value > DIRECTOR_APPROVAL_CARGO
    ):
        state["status"] = "DIRECTOR_APPROVAL_REQUIRED"

    # Medium Risk
    else:
        state["status"] = "MANAGER_APPROVAL_REQUIRED"

    return state


# -------------------------------------------------
# EXECUTION AGENT
# -------------------------------------------------

def execute_reroute(state: ShipmentState):

    state["status"] = "REROUTE_COMPLETED"

    return state


# -------------------------------------------------
# ROUTER
# -------------------------------------------------

def planner_router(state: ShipmentState):

    if state["status"] == "AUTO_APPROVED":
        return "execute"

    return END


# -------------------------------------------------
# LANGGRAPH
# -------------------------------------------------

graph = StateGraph(ShipmentState)

graph.add_node("planner", planner)
graph.add_node("execute", execute_reroute)

graph.set_entry_point("planner")

graph.add_conditional_edges(
    "planner",
    planner_router,
    {
        "execute": "execute",
        END: END,
    },
)

graph.add_edge("execute", END)

workflow = graph.compile()


# -------------------------------------------------
# UI
# -------------------------------------------------

st.title("🚢 Autonomous Supply Chain Rerouting")

shipment_id = st.text_input(
    "Shipment ID",
    "SHIP-1001"
)

source = st.text_input(
    "Source Port",
    "Shanghai"
)

destination = st.text_input(
    "Destination Port",
    "Rotterdam"
)

cargo_value = st.number_input(
    "Cargo Value ($)",
    min_value=0.0,
    value=40000.0
)

current_cost = st.number_input(
    "Current Freight Cost ($)",
    min_value=1.0,
    value=10000.0
)

proposed_cost = st.number_input(
    "Alternative Route Cost ($)",
    min_value=1.0,
    value=10800.0
)

if st.button("Evaluate Reroute"):

    state = {
        "shipment_id": shipment_id,
        "source_port": source,
        "destination_port": destination,
        "cargo_value": cargo_value,
        "current_cost": current_cost,
        "proposed_cost": proposed_cost,
        "cost_increase_pct": 0,
        "approved": False,
        "status": "INITIATED"
    }

    result = workflow.invoke(state)

    status = result["status"]

    st.write(
        f"Cost Increase: {result['cost_increase_pct']}%"
    )

    # -------------------------------------------------
    # AUTO EXECUTE
    # -------------------------------------------------

    if status == "REROUTE_COMPLETED":

        st.success(
            f"✅ Auto Executed - Shipment "
            f"{shipment_id} rerouted successfully."
        )

    # -------------------------------------------------
    # MANAGER APPROVAL
    # -------------------------------------------------

    elif status == "MANAGER_APPROVAL_REQUIRED":

        st.warning(
            "⚠ Logistics Manager approval required"
        )

        approve = st.button(
            "Approve as Logistics Manager"
        )

        reject = st.button(
            "Reject"
        )

        if approve:

            result["approved"] = True

            result = execute_reroute(result)

            st.success(
                "✅ Logistics Manager approved reroute"
            )

        if reject:

            st.error(
                "❌ Logistics Manager rejected reroute"
            )

    # -------------------------------------------------
    # DIRECTOR APPROVAL
    # -------------------------------------------------

    elif status == "DIRECTOR_APPROVAL_REQUIRED":

        st.error(
            "🚨 Director approval required"
        )

        approve = st.button(
            "Approve as Director"
        )

        reject = st.button(
            "Reject"
        )

        if approve:

            result["approved"] = True

            result = execute_reroute(result)

            st.success(
                "✅ Director approved reroute"
            )

        if reject:

            st.error(
                "❌ Director rejected reroute"
            )