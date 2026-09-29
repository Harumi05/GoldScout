#ifndef GOLDSCOUT_DEMO_EXECUTION_MQH
#define GOLDSCOUT_DEMO_EXECUTION_MQH

// Hard account-mode boundary for real order submission. No input may turn a
// REAL, CONTEST or unknown account into an allowed execution target.
string GSDE_AccountModeName(const long accountMode)
{
   if(accountMode==ACCOUNT_TRADE_MODE_DEMO) return "DEMO";
   if(accountMode==ACCOUNT_TRADE_MODE_REAL) return "REAL";
   if(accountMode==ACCOUNT_TRADE_MODE_CONTEST) return "CONTEST";
   return "UNKNOWN";
}

bool GSDE_DemoExecutionAllowed(const bool requested,const long accountMode,
                               string &reason)
{
   reason="";
   if(accountMode==ACCOUNT_TRADE_MODE_REAL)
   {
      reason="REAL_ACCOUNT_HARD_BLOCK";
      return false;
   }
   if(accountMode!=ACCOUNT_TRADE_MODE_DEMO)
   {
      reason="NON_DEMO_ACCOUNT_HARD_BLOCK";
      return false;
   }
   if(!requested)
   {
      reason="DEMO_EXECUTION_DISABLED";
      return false;
   }
   reason="DEMO_ACCOUNT_CONFIRMED";
   return true;
}

double GSDE_RemainingDailyBudget(const double dailyBudget,
                                 const double realizedDailyLoss,
                                 const double openRisk)
{
   if(!MathIsValidNumber(dailyBudget) ||
      !MathIsValidNumber(realizedDailyLoss) ||
      !MathIsValidNumber(openRisk))
      return 0.0;
   return MathMax(0.0,MathMax(0.0,dailyBudget)-
      MathMax(0.0,realizedDailyLoss)-MathMax(0.0,openRisk));
}

string GSDE_CloseReason(const long dealReason)
{
   if(dealReason<0) return "UNKNOWN";
   if(dealReason==DEAL_REASON_TP) return "TP";
   if(dealReason==DEAL_REASON_SL) return "SL";
   if(dealReason==DEAL_REASON_CLIENT || dealReason==DEAL_REASON_MOBILE ||
      dealReason==DEAL_REASON_WEB) return "MANUAL";
   if(dealReason==DEAL_REASON_EXPERT) return "EA_CLOSE";
   if(dealReason==DEAL_REASON_SO) return "BROKER";
   return "OTHER";
}

#endif
