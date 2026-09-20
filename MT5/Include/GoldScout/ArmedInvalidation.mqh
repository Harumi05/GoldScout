#ifndef GOLDSCOUT_ARMED_INVALIDATION_MQH
#define GOLDSCOUT_ARMED_INVALIDATION_MQH

// Conservative, score-neutral cancellation policy for an already armed plan.
// It never proposes the opposite direction and never sends an order.
enum GoldScoutArmedInvalidationAction
{
   GOLDSCOUT_KEEP_ARMED=0,
   GOLDSCOUT_CANCEL_ARMED=1
};

struct GoldScoutArmedInvalidationDecision
{
   GoldScoutArmedInvalidationAction action;
   string reason;
   int    breakoutDirection;
   double rsi;
   double impulseAtr;
};

void GSAR_ClearDecision(GoldScoutArmedInvalidationDecision &decision)
{
   decision.action=GOLDSCOUT_KEEP_ARMED;
   decision.reason="NOT_EVALUATED";
   decision.breakoutDirection=0;
   decision.rsi=0.0;
   decision.impulseAtr=0.0;
}

string GSAR_DirectionName(const int direction)
{
   if(direction>0) return "LONG";
   if(direction<0) return "SHORT";
   return "NONE";
}

GoldScoutArmedInvalidationAction GSAR_EvaluateArmedSetupInvalidation(
   const bool enabled,
   const bool realAccount,
   const bool armed,
   const int armedDirection,
   const bool breakoutConfirmed,
   const int breakoutDirection,
   const double rsi,
   const double signedImpulseAtr,
   const double minimumImpulseAtr,
   GoldScoutArmedInvalidationDecision &decision)
{
   GSAR_ClearDecision(decision);
   decision.breakoutDirection=breakoutDirection;
   decision.rsi=rsi;
   decision.impulseAtr=MathAbs(signedImpulseAtr);

   if(!enabled)
   {
      decision.reason="FEATURE_DISABLED";
      return decision.action;
   }
   if(realAccount)
   {
      decision.reason="REAL_ACCOUNT_NO_EFFECT";
      return decision.action;
   }
   if(!armed || (armedDirection!=1 && armedDirection!=-1))
   {
      decision.reason="NO_ARMED_SETUP";
      return decision.action;
   }
   if(!breakoutConfirmed || breakoutDirection!=-armedDirection)
   {
      decision.reason="NO_CONFIRMED_COUNTER_BREAKOUT";
      return decision.action;
   }

   bool rsiConfirmed=(armedDirection<0 ? rsi>=55.0 : rsi<=45.0);
   if(!MathIsValidNumber(rsi) || !rsiConfirmed)
   {
      decision.reason="RSI_NOT_CONFIRMED";
      return decision.action;
   }

   // signedImpulseAtr is positive for an upward displacement and negative for
   // a downward displacement. It must agree with the confirmed counter-breakout.
   double alignedImpulse=(double)breakoutDirection*signedImpulseAtr;
   if(!MathIsValidNumber(signedImpulseAtr) || minimumImpulseAtr<=0.0 ||
      alignedImpulse+1e-9<minimumImpulseAtr)
   {
      decision.reason="IMPULSE_BELOW_THRESHOLD";
      return decision.action;
   }

   decision.action=GOLDSCOUT_CANCEL_ARMED;
   decision.reason="COUNTER_BREAKOUT_CONFIRMED";
   return decision.action;
}

#endif
