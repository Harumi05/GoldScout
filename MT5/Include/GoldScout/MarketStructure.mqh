#ifndef GOLDSCOUT_MARKET_STRUCTURE_MQH
#define GOLDSCOUT_MARKET_STRUCTURE_MQH

// Confirmed-pivot foundation. This module is intentionally not connected to
// GoldScout's signal or scoring path yet.
enum GoldScoutPivotType
{
   GOLDSCOUT_PIVOT_LOW  = -1,
   GOLDSCOUT_PIVOT_NONE = 0,
   GOLDSCOUT_PIVOT_HIGH = 1
};

struct GoldScoutPivot
{
   GoldScoutPivotType type;
   double             price;
   int                shift;
   datetime           time;
   double             atr;
   bool               confirmed;
};

struct GoldScoutPivotConfig
{
   int    leftBars;
   int    rightBars;
   int    minBarsBetween;
   double minProminenceAtr;
   double toleranceAtr;
};

void GS_ClearPivots(GoldScoutPivot &pivots[])
{
   ArrayResize(pivots,0);
}

bool GS_ValidPositiveNumber(const double value)
{
   return MathIsValidNumber(value) && value>0.0;
}

bool GS_ValidatePivotConfig(const GoldScoutPivotConfig &config)
{
   return config.leftBars>=1 &&
          config.rightBars>=1 &&
          config.minBarsBetween>=1 &&
          MathIsValidNumber(config.minProminenceAtr) &&
          config.minProminenceAtr>=0.0 &&
          GS_ValidPositiveNumber(config.toleranceAtr);
}

// Shared future tolerance for matching structural levels (for example, two
// lows or two highs). Both ATR inputs and the price step must be trustworthy.
bool GS_ATRTolerance(const double firstAtr,const double secondAtr,
                     const double multiplier,const double minimumPriceStep,
                     double &tolerance)
{
   tolerance=0.0;
   if(!GS_ValidPositiveNumber(firstAtr) ||
      !GS_ValidPositiveNumber(secondAtr) ||
      !GS_ValidPositiveNumber(multiplier) ||
      !GS_ValidPositiveNumber(minimumPriceStep))
      return false;

   tolerance=MathMax(minimumPriceStep,MathMax(firstAtr,secondAtr)*multiplier);
   return GS_ValidPositiveNumber(tolerance);
}

// closedRates and closedAtr must be ordered from oldest to newest and must not
// contain the currently open candle. newestClosedShift therefore cannot be 0.
bool GS_DetectConfirmedPivots(const MqlRates &closedRates[],
                              const double &closedAtr[],
                              const int newestClosedShift,
                              const GoldScoutPivotConfig &config,
                              GoldScoutPivot &pivots[])
{
   GS_ClearPivots(pivots);
   if(newestClosedShift<1 || !GS_ValidatePivotConfig(config)) return false;

   int count=ArraySize(closedRates);
   if(count!=ArraySize(closedAtr)) return false;
   for(int i=0;i<count;i++)
   {
      if(closedRates[i].time<=0 ||
         !MathIsValidNumber(closedRates[i].high) ||
         !MathIsValidNumber(closedRates[i].low) ||
         closedRates[i].high<closedRates[i].low ||
         !GS_ValidPositiveNumber(closedAtr[i]))
         return false;
      if(i>0 && closedRates[i].time<=closedRates[i-1].time) return false;
   }

   if(count<config.leftBars+config.rightBars+1) return true;

   int lastAcceptedIndex=-1;
   for(int i=config.leftBars;i<count-config.rightBars;i++)
   {
      bool isHigh=true;
      bool isLow=true;
      double neighbourHigh=closedRates[i-config.leftBars].high;
      double neighbourLow=closedRates[i-config.leftBars].low;

      for(int j=i-config.leftBars;j<=i+config.rightBars;j++)
      {
         if(j==i) continue;
         if(closedRates[i].high<=closedRates[j].high) isHigh=false;
         if(closedRates[i].low>=closedRates[j].low) isLow=false;
         neighbourHigh=MathMax(neighbourHigh,closedRates[j].high);
         neighbourLow=MathMin(neighbourLow,closedRates[j].low);
      }

      double requiredProminence=config.minProminenceAtr*closedAtr[i];
      if(isHigh && closedRates[i].high-neighbourHigh+1e-12<requiredProminence) isHigh=false;
      if(isLow && neighbourLow-closedRates[i].low+1e-12<requiredProminence) isLow=false;

      // An outside bar that qualifies as both directions is ambiguous. Keep the
      // detector fail-closed by emitting neither pivot.
      if(isHigh==isLow) continue;
      if(lastAcceptedIndex>=0 && i-lastAcceptedIndex<config.minBarsBetween) continue;

      int size=ArraySize(pivots);
      if(ArrayResize(pivots,size+1)!=size+1)
      {
         GS_ClearPivots(pivots);
         return false;
      }

      pivots[size].type=isHigh?GOLDSCOUT_PIVOT_HIGH:GOLDSCOUT_PIVOT_LOW;
      pivots[size].price=isHigh?closedRates[i].high:closedRates[i].low;
      pivots[size].shift=newestClosedShift+(count-1-i);
      pivots[size].time=closedRates[i].time;
      pivots[size].atr=closedAtr[i];
      pivots[size].confirmed=true;
      lastAcceptedIndex=i;
   }
   return true;
}

// Terminal adapter: start_pos=1 explicitly excludes the open candle. CopyRates
// and CopyBuffer populate physical memory oldest-to-newest for this detector.
bool GS_LoadConfirmedPivots(const string symbol,
                            const ENUM_TIMEFRAMES timeframe,
                            const int atrHandle,
                            const int barsToLoad,
                            const GoldScoutPivotConfig &config,
                            GoldScoutPivot &pivots[])
{
   GS_ClearPivots(pivots);
   if(symbol=="" || atrHandle==INVALID_HANDLE ||
      barsToLoad<config.leftBars+config.rightBars+1 ||
      !GS_ValidatePivotConfig(config))
      return false;

   MqlRates closedRates[];
   double closedAtr[];
   ArraySetAsSeries(closedRates,false);
   ArraySetAsSeries(closedAtr,false);

   int copied=CopyRates(symbol,timeframe,1,barsToLoad,closedRates);
   if(copied<config.leftBars+config.rightBars+1) return false;
   if(ArrayResize(closedAtr,copied)!=copied) return false;
   if(CopyBuffer(atrHandle,0,1,copied,closedAtr)!=copied) return false;

   return GS_DetectConfirmedPivots(closedRates,closedAtr,1,config,pivots);
}

#endif
