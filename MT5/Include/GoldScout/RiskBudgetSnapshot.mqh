#ifndef GOLDSCOUT_RISK_BUDGET_SNAPSHOT_MQH
#define GOLDSCOUT_RISK_BUDGET_SNAPSHOT_MQH

// Phase 1: read-only evidence. No terminal reads, reservations, order calls or
// acceptance policy live here. Amounts are supplied by the existing EA helpers.
struct GoldScoutRiskSnapshot
{
   datetime observedAt;
   string accountCurrency;
   int brokerDay;
   bool persistentStateKnown;
   datetime persistentStateCheckedAt;
   bool equityKnown;
   bool openRiskKnown;
   bool h1Known;
   bool h1LocalEntryUsed;
   double startEquity;
   double equity;
   double riskPercent;
   double targetRisk;
   double dailyPercent;
   double dailyBudget;
   double realizedLoss;
   double openRisk;
   double remaining;
   double plannedRisk;
   bool candidateRiskKnown;
   double candidatePlannedRisk;
   datetime candidateEvaluatedAt;
   bool drawdownKnown;
   double drawdown;
   double maxDrawdown;
   datetime h1Bar;
   string h1State;
   string blockCode;
   string blockReason;
};

string GSRB_Escape(string value)
{
   StringReplace(value,"\\","\\\\");
   StringReplace(value,"\"","\\\"");
   StringReplace(value,"\r"," ");
   StringReplace(value,"\n"," ");
   StringReplace(value,"\t"," ");
   return value;
}

string GSRB_Number(const bool known,const double value,const int digits=2)
{
   return known && MathIsValidNumber(value)?DoubleToString(value,digits):"null";
}

string GSRB_DailyBlockCode(const bool stateKnown,const bool openRiskKnown,
                          const double remaining,const double realizedLoss,
                          const double openRisk)
{
   if(!stateKnown) return "RISK_STATE_UNKNOWN";
   if(!openRiskKnown) return "OPEN_RISK_UNKNOWN";
   if(remaining>0.0) return "NONE";
   if(realizedLoss>0.0 && openRisk>0.0)
      return "DAILY_BUDGET_EXHAUSTED_COMBINED";
   if(realizedLoss>0.0) return "DAILY_BUDGET_EXHAUSTED_REALIZED_LOSS";
   if(openRisk>0.0) return "DAILY_BUDGET_EXHAUSTED_OPEN_RISK";
   return "DAILY_BUDGET_EXHAUSTED";
}

string GSRB_Json(const GoldScoutRiskSnapshot &s)
{
   bool budgetKnown=s.persistentStateKnown && s.openRiskKnown;
   bool healthy=budgetKnown && s.equityKnown && s.drawdownKnown && s.h1Known;
   string json="{\"schema_version\":1,\"observer_only\":true,\"score_effect\":0,";
   json+=StringFormat("\"observed_at\":%I64d,\"broker_day\":%d,",(long)s.observedAt,s.brokerDay);
   json+="\"account_currency\":\""+GSRB_Escape(s.accountCurrency)+"\",";
   json+="\"start_of_day_equity\":"+GSRB_Number(s.persistentStateKnown,s.startEquity)+",";
   json+="\"current_equity\":"+GSRB_Number(s.equityKnown,s.equity)+",";
   json+="\"configured_risk_percent\":"+GSRB_Number(true,s.riskPercent)+",";
   json+="\"target_risk_amount\":"+GSRB_Number(s.equityKnown,s.targetRisk)+",";
   json+="\"daily_loss_limit_percent\":"+GSRB_Number(true,s.dailyPercent)+",";
   json+="\"daily_budget_amount\":"+GSRB_Number(s.persistentStateKnown,s.dailyBudget)+",";
   json+="\"realized_daily_loss_used\":"+GSRB_Number(s.persistentStateKnown,s.realizedLoss)+",";
   json+="\"realized_loss_semantics\":\"SUM_NEGATIVE_NET_DEALS_PERSISTENT_MAX\",";
   json+="\"account_open_risk\":"+GSRB_Number(s.openRiskKnown,s.openRisk)+",";
   json+="\"pending_risk_supported\":false,\"pending_risk_status\":\"NOT_SUPPORTED\",\"pending_risk_amount\":null,";
   json+="\"remaining_daily_budget\":"+GSRB_Number(budgetKnown,s.remaining)+",";
   json+="\"planned_risk_amount\":"+GSRB_Number(budgetKnown && s.equityKnown,s.plannedRisk)+",";
   json+="\"candidate_planned_risk_amount\":"+GSRB_Number(s.candidateRiskKnown,s.candidatePlannedRisk)+",";
   json+="\"candidate_evaluated_at\":"+(s.candidateRiskKnown?IntegerToString((long)s.candidateEvaluatedAt):"null")+",";
   json+="\"current_drawdown_percent\":"+GSRB_Number(s.drawdownKnown,s.drawdown)+",";
   json+="\"max_drawdown_percent\":"+GSRB_Number(true,s.maxDrawdown)+",";
   json+="\"h1_bar\":"+(s.h1Bar>0?IntegerToString((long)s.h1Bar):"null")+",";
   json+="\"h1_reservation_state\":\""+GSRB_Escape(s.h1State)+"\",";
   json+="\"persistent_state_known\":"+(s.persistentStateKnown?"true":"false")+",";
   json+=StringFormat("\"persistent_state_checked_at\":%I64d,",(long)s.persistentStateCheckedAt);
   json+="\"h1_local_entry_used\":"+(s.h1LocalEntryUsed?"true":"false")+",";
   json+="\"open_risk_known\":"+(s.openRiskKnown?"true":"false")+",";
   json+="\"risk_state_known\":"+(healthy?"true":"false")+",";
   json+="\"risk_state_health\":\""+(healthy?"HEALTHY":"UNKNOWN")+"\",";
   json+="\"budget_status_code\":\""+GSRB_DailyBlockCode(s.persistentStateKnown,
      s.openRiskKnown,s.remaining,s.realizedLoss,s.openRisk)+"\",";
   json+="\"block_code\":\""+GSRB_Escape(s.blockCode)+"\",";
   json+="\"block_reason\":\""+GSRB_Escape(s.blockReason)+"\"}";
   return json;
}

#endif
