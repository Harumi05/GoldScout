#property strict
#property script_show_inputs

#include <GoldScout/MarketObserver.mqh>

// MetaEditor compile harness: it exercises the public observer types without
// requiring a broker connection or invoking any trading API.
void OnStart()
{
   GoldScoutObserverContext context;
   context.session="LONDRES";
   context.newsBias=0;
   context.newsDataRisk="LOW";
   context.longScore=60;
   context.shortScore=20;
   context.decision="Sin setup";
   context.decisionReason="Score insuficiente";
   context.monitorState="SIN SETUP";
   context.direction="NONE";
   context.liveTrading=false;
   context.armedInvalidationStatus="DISABLED";
   context.previousArmedDirection="NONE";
   context.previousArmedSetup="-";
   context.previousArmedScore=0;
   context.armedInvalidationReason="FEATURE_DISABLED";
   context.armedInvalidationBreakoutDirection="NONE";
   context.armedInvalidationRsi=0.0;
   context.armedInvalidationImpulseAtr=0.0;
   context.armedInvalidationStructure="INSUFICIENTE";
   context.tpEvaluated=false;
   context.currentTP=0.0;
   context.currentRR=0.0;
   context.v2TP=0.0;
   context.v2RR=0.0;
   context.selectedTP=0.0;
   context.selectedRR=0.0;
   context.tpMode="NOT_EVALUATED";
   context.tpStructureLevel=0.0;
   context.tpStructureConfidence="NONE";
   context.executionState="PAPER";
   context.executionReason="DEMO_EXECUTION_DISABLED";
   context.executionRetcode=0;
   context.signalEventId="";

   GoldScoutMarketObserver observer;
   string outcome=GSMO_DecisionOutcome(context);
   if(outcome=="") Print("Unexpected empty observer outcome");
   observer.Shutdown();
}
