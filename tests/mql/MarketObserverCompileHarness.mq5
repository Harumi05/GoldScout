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

   GoldScoutMarketObserver observer;
   string outcome=GSMO_DecisionOutcome(context);
   if(outcome=="") Print("Unexpected empty observer outcome");
   observer.Shutdown();
}
