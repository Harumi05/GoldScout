#property strict
#property version   "1.00"
#property description "GoldScout H1 - XAUUSD autonomous EA with account-currency risk, trend/pullback scoring, news filter and dashboard JSON."

#include <Trade/Trade.mqh>
#include <GoldScout/MarketStructure.mqh>

CTrade trade;

input group "=== Execution ==="
input bool   EnableLiveTrading      = false; // FALSE = analysis only / paper mode
input long   MagicNumber             = 8202609;
input int    TimerSeconds            = 5;
input int    MinScoreToTrade         = 74;
input bool   OnePositionAtATime      = true;
input bool   OneDecisionPerHour      = true; // compatibilidad; ya no bloquea el monitoreo intrabar
input bool   UseIntrabarMonitoring   = true;
input bool   DebugIntrabarLogs       = false;
input int    ArmScoreThreshold       = 58; // score minimo para armar un setup durante la vela


input group "=== Risk / Targets ==="
input double RiskPercent             = 5.0;  // risk as % of current equity per trade
input double ChillTargetR            = 1.25; // weaker structure: target >= 1.25R
input double GodTargetR              = 2.0;  // clear trend: target >= 2R
input double DailyLossLimitUSD       = 20.0; // legacy name: value is in account deposit currency
input double MaxDrawdownPercent      = 15.0; // equity drawdown from peak
input double MaxSpreadUSD            = 0.60; // max bid/ask spread on gold
input double RoundTurnCommissionPerLot = 0.0; // optional account-currency cost buffer per lot
input double ATRStopMultiplier       = 1.20; // base stop distance from ATR
input double MinStopATR              = 0.80; // minimum stop distance in ATR
input double MaxStopATR              = 2.00; // maximum stop distance in ATR
input double StructureBufferATR      = 0.25; // buffer beyond recent swing
input int    StructureLookback       = 20;   // H1 swing lookback for stop placement
input double MinRewardRisk           = 1.25; // minimum R:R allowed

input group "=== Indicators ==="
input int    FastEMA                 = 20;
input int    SlowEMA                 = 50;
input int    HTFFastEMA              = 50;
input int    HTFSlowEMA              = 200;
input int    RSIPeriod               = 14;
input int    ADXPeriod               = 14;
input int    ATRPeriod               = 14;
input int    VolumeLookback          = 20;
input double MinADX                  = 20.0;

input group "=== Confirmed Market Structure ==="
input int    PivotLookbackBars       = 120;
input int    PivotLeftBars           = 2;
input int    PivotRightBars          = 2;
input int    PivotMinBarsBetween     = 2;
input double PivotMinProminenceATR   = 0.20;
input double PivotEqualityToleranceATR = 0.20;

input group "=== Diagnostic W/M Patterns ==="
input bool   EnablePatternDiagnostics       = true;
input double PatternExtremeToleranceATR     = 0.30;
input double PatternMinDepthATR             = 1.00;
input int    PatternMinPivotBars             = 3;
input int    PatternMaxPivotBars             = 36;
input int    PatternMaxConfirmationBars      = 24;
input double PatternBreakoutBufferATR        = 0.05;
input double PatternInvalidationATR          = 0.25;
input double PatternMinLegBalance            = 0.25;
input bool   PatternUseVolumeQuality         = true;
input int    PatternVolumeLookback           = 20;
input double PatternVolumeMultiplier         = 1.05;
input bool   PatternUseMomentumQuality       = true;
input double PatternMomentumBodyATR          = 0.50;
input bool   DebugStructurePatternLogs       = false;

input group "=== Diagnostic Flags / Pennants ==="
input bool   EnableContinuationPatternDiagnostics = true;
input double ContinuationMinPoleATR          = 3.00;
input int    ContinuationMinPoleBars          = 2;
input int    ContinuationMaxPoleBars          = 24;
input double ContinuationMinPoleEfficiency   = 0.60;
input double ContinuationMinRetracement      = 0.10;
input double ContinuationMaxRetracement      = 0.60;
input int    ContinuationMinBars              = 4;
input int    ContinuationMaxBars              = 24;
input int    ContinuationMaxConfirmationBars  = 12;
input double ContinuationMinGeometryMoveATR  = 0.20;
input double FlagMinParallelRatio             = 0.50;
input double FlagWidthTolerance               = 0.50;
input double PennantMaxWidthRatio             = 0.75;
input double PennantMinConvergenceBalance     = 0.25;
input double ContinuationBreakoutBufferATR    = 0.05;
input double ContinuationInvalidationATR      = 0.20;
input bool   ContinuationUseVolumeQuality     = true;
input int    ContinuationVolumeLookback       = 20;
input double ContinuationVolumeMultiplier     = 1.05;
input bool   ContinuationUseMomentumQuality   = true;
input double ContinuationMomentumBodyATR      = 0.50;
input bool   DebugContinuationPatternLogs     = false;

input group "=== News Filter ==="
input bool   UseNewsFilter           = true;
input int    NewsBlockBeforeMin      = 30;
input int    NewsBlockAfterMin       = 30;
input bool   UseWorldNewsAnalysis     = true; // requiere archivo actualizado por dashboard/news_service.py
input bool   RequireFreshWorldNews    = true;
input int    MaxNewsAgeMinutes        = 15;
input bool   BlockHighNewsRisk        = false; // news risk informs context; MT5 calendar still blocks scheduled events
input int    NewsScoreMaxPoints       = 10;

input group "=== Dashboard ==="
input string DashboardFile           = "xau_goldscout_dashboard.json";
input bool   WriteDashboard           = true;

int hEmaFast = INVALID_HANDLE;
int hEmaSlow = INVALID_HANDLE;
int hEmaHTFFast = INVALID_HANDLE;
int hEmaHTFSlow = INVALID_HANDLE;
int hRSI = INVALID_HANDLE;
int hADX = INVALID_HANDLE;
int hATR = INVALID_HANDLE;

datetime g_lastH1Bar = 0;
double   g_peakEquity = 0.0;
datetime g_dayStart = 0;
const double HARD_MAX_RISK_PERCENT = 5.0;
const int    MAX_EXECUTION_DEVIATION_POINTS = 30; // existing execution tolerance; included in planned-risk sizing
const int    MAX_TERMINAL_GLOBAL_NAME_LENGTH = 63;
double   g_dailyLossUsed = 0.0;
string   g_lastDecision = "Inicializando...";
string   g_lastSetup = "-";
int      g_lastScore = 0;
string   g_lastDirection = "-";
double   g_tempSL = 0.0;
int      g_newsBias = 0;
int      g_newsConfidence = 0;
string   g_newsRisk = "UNKNOWN";
string   g_newsDirection = "NEUTRO";
string   g_newsSummary = "Sin análisis de noticias.";
int      g_newsCount = 0;
bool     g_newsAvailable = false;
string   g_newsUpdated = "";
long     g_newsUpdatedEpoch = 0;

// Detailed H1 diagnostics for Experts + dashboard
double g_diagEmaFast=0.0, g_diagEmaSlow=0.0, g_diagH4Fast=0.0, g_diagH4Slow=0.0;
double g_diagRSI=0.0, g_diagADX=0.0, g_diagATR=0.0, g_diagCurVol=0.0, g_diagAvgVol=0.0;
int g_diagLongTech=0, g_diagShortTech=0, g_diagLongFinal=0, g_diagShortFinal=0;
int g_diagLongNewsPts=0, g_diagShortNewsPts=0;
string g_diagH4Trend="NEUTRA", g_diagH1Trend="NEUTRA";
string g_diagStructure="-", g_diagVolume="-";
string g_diagTechnicalReason="";
string g_diagNewsReason="";
string g_diagBlockReason="";
datetime g_diagBar=0;

// Intrabar monitoring state
bool     g_armed=false;
bool     g_entryUsedThisBar=false;
int      g_armedDirection=0;
int      g_armedScore=0;
string   g_armedSetup="-";
string   g_monitorState="SIN PLAN";
string   g_monitorReason="Esperando nueva vela H1.";
long     g_monitorAgeSec=0;
bool     g_startupAnalysisPending=true;
int      g_startupAttempts=0;
int      g_intrabarLongBoost=0;
int      g_intrabarShortBoost=0;
datetime g_lastIntrabarHeartbeat=0;
const int INTRABAR_DIAGNOSTIC_INTERVAL_SECONDS=30;

// W/M diagnostics never participate in scoring or execution.
GoldScoutPatternDiagnostic g_patternDiagnostic;
datetime g_patternEvaluatedClosedBar=0;
datetime g_lastPatternLogTime=0;
string   g_lastPatternLogSignature="";
const int STRUCTURE_PATTERN_LOG_INTERVAL_SECONDS=30;

GoldScoutContinuationPatternDiagnostic g_continuationPatternDiagnostic;
datetime g_continuationPatternEvaluatedClosedBar=0;
datetime g_lastContinuationPatternLogTime=0;
string   g_lastContinuationPatternLogSignature="";
const int CONTINUATION_PATTERN_LOG_INTERVAL_SECONDS=30;

struct BrokerContractSpec
{
   string accountCurrency;
   string profitCurrency;
   int    priceDigits;
   double point;
   double tickSize;
   double tickValue;
   double tickValueProfit;
   double tickValueLoss;
   double contractSize;
   double volumeMin;
   double volumeMax;
   double volumeStep;
   int    stopsLevel;
   int    freezeLevel;
};

enum EntryReservationPhase
{
   ENTRY_RESERVATION_NONE=0,
   ENTRY_RESERVATION_PENDING=1,
   ENTRY_RESERVATION_CONFIRMED=2
};

string JsonEscape(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\r", " ");
   StringReplace(s, "\n", " ");
   return s;
}

bool GetValue(int handle, int buffer, int shift, double &out)
{
   double v[];
   ArraySetAsSeries(v, true);
   if(handle == INVALID_HANDLE) return false;
   if(CopyBuffer(handle, buffer, shift, 1, v) != 1) return false;
   out = v[0];
   return true;
}

void ConfigurePivotEngine(GoldScoutPivotConfig &config)
{
   config.leftBars=PivotLeftBars;
   config.rightBars=PivotRightBars;
   config.minBarsBetween=PivotMinBarsBetween;
   config.minProminenceAtr=PivotMinProminenceATR;
   config.toleranceAtr=PivotEqualityToleranceATR;
}

void ConfigurePatternDiagnostics(GoldScoutPatternConfig &config)
{
   config.extremeToleranceAtr=PatternExtremeToleranceATR;
   config.minDepthAtr=PatternMinDepthATR;
   config.minPivotBars=PatternMinPivotBars;
   config.maxPivotBars=PatternMaxPivotBars;
   config.maxConfirmationBars=PatternMaxConfirmationBars;
   config.breakoutBufferAtr=PatternBreakoutBufferATR;
   config.invalidationAtr=PatternInvalidationATR;
   config.minLegBalance=PatternMinLegBalance;
   config.useVolumeQuality=PatternUseVolumeQuality;
   config.volumeLookback=PatternVolumeLookback;
   config.volumeMultiplier=PatternVolumeMultiplier;
   config.useMomentumQuality=PatternUseMomentumQuality;
   config.momentumBodyAtr=PatternMomentumBodyATR;
}

void LogStructurePattern(const GoldScoutPatternDiagnostic &pattern)
{
   if(!DebugStructurePatternLogs || !pattern.detected) return;
   string signature=pattern.identity+"|"+GS_PatternStateName(pattern.state);
   if(signature==g_lastPatternLogSignature) return;

   datetime now=TimeTradeServer();
   if(now<=0) now=TimeLocal();
   if(now<=0 || (g_lastPatternLogTime>0 &&
      (long)(now-g_lastPatternLogTime)<STRUCTURE_PATTERN_LOG_INTERVAL_SECONDS))
      return;

   string firstLabel=pattern.type==GOLDSCOUT_PATTERN_W?"low1":"high1";
   string secondLabel=pattern.type==GOLDSCOUT_PATTERN_W?"low2":"high2";
   PrintFormat("[GoldScout][STRUCTURE] pattern=%s | state=%s | %s=%.2f | neckline=%.2f | %s=%.2f | quality=%.1f (%s)",
      GS_PatternTypeName(pattern.type),GS_PatternStateName(pattern.state),
      firstLabel,pattern.first.price,pattern.neckline.price,secondLabel,
      pattern.second.price,pattern.quality,GS_PatternQualityName(pattern.quality));
   g_lastPatternLogSignature=signature;
   g_lastPatternLogTime=now;
}

void RefreshStructurePatternDiagnostics(const GoldScoutPivot &confirmedPivots[],
                                        const bool pivotDataAvailable)
{
   if(!EnablePatternDiagnostics)
   {
      GS_ClearPatternDiagnostic(g_patternDiagnostic);
      return;
   }

   datetime latestClosedBar=iTime(_Symbol,PERIOD_H1,1);
   if(latestClosedBar<=0 || latestClosedBar==g_patternEvaluatedClosedBar) return;

   GoldScoutPatternConfig config;
   ConfigurePatternDiagnostics(config);
   GoldScoutPatternDiagnostic detectedPattern;
   GS_ClearPatternDiagnostic(detectedPattern);
   if(!pivotDataAvailable ||
      !GS_LoadLatestPatternDiagnostic(_Symbol,PERIOD_H1,PivotLookbackBars,
         confirmedPivots,_Point,config,detectedPattern))
   {
      GS_ClearPatternDiagnostic(g_patternDiagnostic);
      return;
   }

   g_patternDiagnostic=detectedPattern;
   g_patternEvaluatedClosedBar=latestClosedBar;
   LogStructurePattern(g_patternDiagnostic);
}

void ConfigureContinuationPatternDiagnostics(GoldScoutContinuationPatternConfig &config)
{
   config.minPoleAtr=ContinuationMinPoleATR;
   config.minPoleBars=ContinuationMinPoleBars;
   config.maxPoleBars=ContinuationMaxPoleBars;
   config.minPoleEfficiency=ContinuationMinPoleEfficiency;
   config.minRetracementRatio=ContinuationMinRetracement;
   config.maxRetracementRatio=ContinuationMaxRetracement;
   config.minConsolidationBars=ContinuationMinBars;
   config.maxConsolidationBars=ContinuationMaxBars;
   config.maxConfirmationBars=ContinuationMaxConfirmationBars;
   config.minGeometryMoveAtr=ContinuationMinGeometryMoveATR;
   config.minFlagParallelRatio=FlagMinParallelRatio;
   config.flagWidthTolerance=FlagWidthTolerance;
   config.maxPennantWidthRatio=PennantMaxWidthRatio;
   config.minPennantConvergenceBalance=PennantMinConvergenceBalance;
   config.breakoutBufferAtr=ContinuationBreakoutBufferATR;
   config.invalidationAtr=ContinuationInvalidationATR;
   config.useVolumeQuality=ContinuationUseVolumeQuality;
   config.volumeLookback=ContinuationVolumeLookback;
   config.volumeMultiplier=ContinuationVolumeMultiplier;
   config.useMomentumQuality=ContinuationUseMomentumQuality;
   config.momentumBodyAtr=ContinuationMomentumBodyATR;
}

void LogContinuationPattern(const GoldScoutContinuationPatternDiagnostic &pattern)
{
   if(!DebugContinuationPatternLogs || !pattern.detected) return;
   string signature=pattern.identity+"|"+GS_PatternStateName(pattern.state);
   if(signature==g_lastContinuationPatternLogSignature) return;

   datetime now=TimeTradeServer();
   if(now<=0) now=TimeLocal();
   if(now<=0 || (g_lastContinuationPatternLogTime>0 &&
      (long)(now-g_lastContinuationPatternLogTime)<CONTINUATION_PATTERN_LOG_INTERVAL_SECONDS))
      return;

   PrintFormat("[GoldScout][STRUCTURE] pattern=%s | state=%s | pole=%.2fATR | retracement=%.1f%% | bars=%d | quality=%.1f (%s)",
      GS_ContinuationPatternTypeName(pattern.type),GS_PatternStateName(pattern.state),
      pattern.poleStrengthAtr,pattern.retracementRatio*100.0,
      pattern.consolidationBars,pattern.quality,GS_PatternQualityName(pattern.quality));
   g_lastContinuationPatternLogSignature=signature;
   g_lastContinuationPatternLogTime=now;
}

void RefreshContinuationPatternDiagnostics(const GoldScoutPivot &confirmedPivots[],
                                           const bool pivotDataAvailable)
{
   if(!EnableContinuationPatternDiagnostics)
   {
      GS_ClearContinuationPatternDiagnostic(g_continuationPatternDiagnostic);
      return;
   }

   datetime latestClosedBar=iTime(_Symbol,PERIOD_H1,1);
   if(latestClosedBar<=0 || latestClosedBar==g_continuationPatternEvaluatedClosedBar) return;

   GoldScoutContinuationPatternConfig config;
   ConfigureContinuationPatternDiagnostics(config);
   GoldScoutContinuationPatternDiagnostic detectedPattern;
   GS_ClearContinuationPatternDiagnostic(detectedPattern);
   if(!pivotDataAvailable ||
      !GS_LoadLatestContinuationPatternDiagnostic(_Symbol,PERIOD_H1,PivotLookbackBars,
         confirmedPivots,_Point,config,detectedPattern))
   {
      GS_ClearContinuationPatternDiagnostic(g_continuationPatternDiagnostic);
      return;
   }

   g_continuationPatternDiagnostic=detectedPattern;
   g_continuationPatternEvaluatedClosedBar=latestClosedBar;
   LogContinuationPattern(g_continuationPatternDiagnostic);
}

bool LoadBrokerContract(BrokerContractSpec &spec,string &msg)
{
   msg="";
   ResetLastError();
   spec.accountCurrency=AccountInfoString(ACCOUNT_CURRENCY);
   if(GetLastError()!=0 || spec.accountCurrency=="")
   {
      msg="moneda de depósito no disponible";
      return false;
   }
   if(!SymbolInfoString(_Symbol,SYMBOL_CURRENCY_PROFIT,spec.profitCurrency) || spec.profitCurrency=="")
   {
      msg="moneda de beneficio del símbolo no disponible";
      return false;
   }

   long priceDigits=0,stopsLevel=0,freezeLevel=0;
   if(!SymbolInfoInteger(_Symbol,SYMBOL_DIGITS,priceDigits) ||
      !SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL,stopsLevel) ||
      !SymbolInfoInteger(_Symbol,SYMBOL_TRADE_FREEZE_LEVEL,freezeLevel) ||
      !SymbolInfoDouble(_Symbol,SYMBOL_POINT,spec.point) ||
      !SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE,spec.tickSize) ||
      !SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE,spec.tickValue) ||
      !SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE_PROFIT,spec.tickValueProfit) ||
      !SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE_LOSS,spec.tickValueLoss) ||
      !SymbolInfoDouble(_Symbol,SYMBOL_TRADE_CONTRACT_SIZE,spec.contractSize) ||
      !SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN,spec.volumeMin) ||
      !SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MAX,spec.volumeMax) ||
      !SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP,spec.volumeStep))
   {
      msg="especificaciones incompletas del símbolo";
      return false;
   }

   spec.priceDigits=(int)priceDigits;
   spec.stopsLevel=(int)stopsLevel;
   spec.freezeLevel=(int)freezeLevel;
   if(spec.priceDigits<0 || spec.point<=0.0 || spec.tickSize<=0.0 ||
      spec.tickValue<=0.0 || spec.tickValueProfit<=0.0 || spec.tickValueLoss<=0.0 ||
      spec.contractSize<=0.0 || spec.volumeMin<=0.0 ||
      spec.volumeMax<spec.volumeMin || spec.volumeStep<=0.0 ||
      spec.stopsLevel<0 || spec.freezeLevel<0)
   {
      msg="especificaciones inválidas del contrato XAUUSD";
      return false;
   }
   return true;
}

double NormalizeVolumeDown(double lots,const BrokerContractSpec &spec)
{
   if(lots<spec.volumeMin-1e-9 || spec.volumeStep<=0.0) return 0.0;
   lots=MathMax(lots,spec.volumeMin);
   lots=MathMin(lots,spec.volumeMax);
   double steps=MathFloor((lots-spec.volumeMin)/spec.volumeStep+1e-9);
   double normalized=spec.volumeMin+steps*spec.volumeStep;
   if(normalized<spec.volumeMin-1e-9 || normalized>spec.volumeMax+1e-9 || normalized>lots+1e-9) return 0.0;
   return NormalizeDouble(normalized, 8);
}

double AlignPriceToTick(const double price,const BrokerContractSpec &spec,const bool roundUp)
{
   if(price<=0.0 || spec.tickSize<=0.0) return 0.0;
   double ticks=price/spec.tickSize;
   double aligned=(roundUp?MathCeil(ticks-1e-9):MathFloor(ticks+1e-9))*spec.tickSize;
   return NormalizeDouble(aligned,spec.priceDigits);
}

bool GetPositionAccountingMode(bool &isHedging)
{
   isHedging=false;
   ResetLastError();
   long mode=AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   if(GetLastError()!=0) return false;
   if(mode!=ACCOUNT_MARGIN_MODE_RETAIL_NETTING &&
      mode!=ACCOUNT_MARGIN_MODE_EXCHANGE &&
      mode!=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) return false;
   isHedging=(mode==ACCOUNT_MARGIN_MODE_RETAIL_HEDGING);
   return true;
}

bool FindMatchingOpenPosition(const bool requireOurMagic,bool &found)
{
   found=false;
   ResetLastError();
   int total=PositionsTotal();
   if(GetLastError()!=0) return false;
   for(int i=0; i<total; i++)
   {
      ResetLastError();
      ulong ticket=PositionGetTicket(i);
      if(ticket==0 || GetLastError()!=0) return false;
      if(!PositionSelectByTicket(ticket)) return false;

      ResetLastError();
      string symbol=PositionGetString(POSITION_SYMBOL);
      long magic=(long)PositionGetInteger(POSITION_MAGIC);
      if(GetLastError()!=0) return false;
      if(symbol!=_Symbol) continue;
      if(requireOurMagic && magic!=MagicNumber) continue;
      found=true;
      return true;
   }
   return true;
}

bool FindMatchingActiveOrder(const bool requireOurMagic,bool &found)
{
   found=false;
   ResetLastError();
   int total=OrdersTotal();
   if(GetLastError()!=0) return false;
   for(int i=0; i<total; i++)
   {
      ResetLastError();
      ulong ticket=OrderGetTicket(i);
      if(ticket==0 || GetLastError()!=0) return false;

      ResetLastError();
      string symbol=OrderGetString(ORDER_SYMBOL);
      long magic=(long)OrderGetInteger(ORDER_MAGIC);
      if(GetLastError()!=0) return false;
      if(symbol!=_Symbol) continue;
      if(requireOurMagic && magic!=MagicNumber) continue;
      found=true;
      return true;
   }
   return true;
}

bool PositionStateAllowsEntry(string &blockReason)
{
   blockReason="";
   bool isHedging=false;
   if(!GetPositionAccountingMode(isHedging))
   {
      blockReason="Bloqueado: no se pudo determinar el modo NETTING/HEDGING";
      return false;
   }

   // NETTING has one shared position per symbol, so any owner must block to
   // avoid increasing, reducing or reversing manual/other-EA exposure. In
   // HEDGING, OnePositionAtATime applies only to this EA's symbol + magic.
   if(isHedging && !OnePositionAtATime) return true;
   bool requireOurMagic=isHedging;
   bool found=false;
   if(!FindMatchingOpenPosition(requireOurMagic,found))
   {
      blockReason="Bloqueado: no se pudo verificar posiciones activas";
      return false;
   }
   if(found)
   {
      blockReason=isHedging
         ? "Esperando: ya existe una posición GoldScout activa"
         : "Bloqueado: existe una posición del símbolo en cuenta NETTING";
      return false;
   }
   if(!FindMatchingActiveOrder(requireOurMagic,found))
   {
      blockReason="Bloqueado: no se pudo verificar órdenes activas";
      return false;
   }
   if(found)
   {
      blockReason=isHedging
         ? "Esperando: ya existe una orden GoldScout activa"
         : "Bloqueado: existe una orden del símbolo en cuenta NETTING";
      return false;
   }
   return true;
}

bool IsOurPosition()
{
   bool found=false;
   return FindMatchingOpenPosition(true,found) && found;
}

bool IsAccountRiskDealType(const ENUM_DEAL_TYPE dealType)
{
   switch(dealType)
   {
      case DEAL_TYPE_BUY:
      case DEAL_TYPE_SELL:
      case DEAL_TYPE_CHARGE:
      case DEAL_TYPE_CORRECTION:
      case DEAL_TYPE_COMMISSION:
      case DEAL_TYPE_COMMISSION_DAILY:
      case DEAL_TYPE_COMMISSION_MONTHLY:
      case DEAL_TYPE_COMMISSION_AGENT_DAILY:
      case DEAL_TYPE_COMMISSION_AGENT_MONTHLY:
      case DEAL_TYPE_INTEREST:
         return true;
   }
   return false;
}

// Returns false instead of treating an unavailable account history as zero P&L.
// The daily-loss guard must fail closed when it cannot establish the account state.
bool TodayAccountProfit(double &total)
{
   total=0.0;
   datetime now=TimeTradeServer();
   if(now<=0) return false;
   MqlDateTime dt;
   if(!TimeToStruct(now,dt)) return false;
   dt.hour=0; dt.min=0; dt.sec=0;
   datetime start=StructToTime(dt);
   if(start<=0) return false;

   ResetLastError();
   if(!HistorySelect(start,now)) return false;
   uint deals=HistoryDealsTotal();
   if(GetLastError()!=0) return false;
   for(uint i=0; i<deals; i++)
   {
      ResetLastError();
      ulong ticket=HistoryDealGetTicket(i);
      if(ticket==0 || GetLastError()!=0) return false;

      ResetLastError();
      ENUM_DEAL_TYPE dealType=(ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket,DEAL_TYPE);
      if(GetLastError()!=0) return false;
      // Include account-wide trading P&L, fees, financing and taxes while
      // excluding deposits, credit and bonuses from the risk budget.
      if(!IsAccountRiskDealType(dealType)) continue;
      ResetLastError();
      double dealResult=HistoryDealGetDouble(ticket,DEAL_PROFIT)
                       + HistoryDealGetDouble(ticket,DEAL_SWAP)
                       + HistoryDealGetDouble(ticket,DEAL_COMMISSION)
                       + HistoryDealGetDouble(ticket,DEAL_FEE);
      if(GetLastError()!=0) return false;
      total += dealResult;
   }
   return true;
}

// Dashboard reporting remains best-effort; execution guards use TodayAccountProfit
// directly and therefore never treat a history error as a safe zero-loss day.
double TodayClosedProfit()
{
   double total=0.0;
   return TodayAccountProfit(total) ? total : 0.0;
}

bool IsSafetyStateKeyValid(const string key)
{
   return StringLen(key)>0 && StringLen(key)<=MAX_TERMINAL_GLOBAL_NAME_LENGTH;
}

// Daily loss and peak equity are account-wide safety invariants. They deliberately
// do not include symbol or magic so every GoldScout instance sees the same state.
string AccountStateIdentity()
{
   return StringFormat("%I64d.%s",(long)AccountInfoInteger(ACCOUNT_LOGIN),AccountInfoString(ACCOUNT_SERVER));
}

string AccountSafetyStateKey(const string suffix)
{
   return StringFormat("GSH2.A.%s.%s",AccountStateIdentity(),suffix);
}

// One-entry-per-H1 is shared by every GoldScout instance on the same broker
// account. It intentionally ignores symbol suffixes and MagicNumber changes.
string EntrySafetyStateKey()
{
   return StringFormat("GSH2.E.%s",AccountStateIdentity());
}

bool SetSafetyStateValue(const string key,const double value)
{
   if(!IsSafetyStateKeyValid(key)) return false;
   ResetLastError();
   datetime changed=GlobalVariableSet(key,value);
   return changed>0 && GetLastError()==0;
}

bool ReadSafetyStateValue(const string key,double &value)
{
   value=0.0;
   if(!IsSafetyStateKeyValid(key) || !GlobalVariableCheck(key)) return false;
   ResetLastError();
   value=GlobalVariableGet(key);
   return GetLastError()==0;
}

bool FlushSafetyState()
{
   ResetLastError();
   GlobalVariablesFlush();
   return GetLastError()==0;
}

bool SafetyInputsValid(string &msg)
{
   msg="";
   if(RiskPercent<0.0 || RiskPercent>HARD_MAX_RISK_PERCENT)
   {
      msg=StringFormat("Configuración inválida: RiskPercent debe estar entre 0 y %.2f",HARD_MAX_RISK_PERCENT);
      return false;
   }
   if(DailyLossLimitUSD<=0.0)
   {
      msg="Configuración inválida: DailyLossLimitUSD debe ser mayor que cero (moneda de cuenta)";
      return false;
   }
   if(RoundTurnCommissionPerLot<0.0)
   {
      msg="Configuración inválida: RoundTurnCommissionPerLot no puede ser negativa";
      return false;
   }
   if(MaxDrawdownPercent<=0.0 || MaxDrawdownPercent>100.0)
   {
      msg="Configuración inválida: MaxDrawdownPercent debe estar entre 0 y 100";
      return false;
   }
   GoldScoutPivotConfig pivotConfig;
   ConfigurePivotEngine(pivotConfig);
   if(!GS_ValidatePivotConfig(pivotConfig) ||
      PivotLookbackBars<pivotConfig.leftBars+pivotConfig.rightBars+1)
   {
      msg="Configuración inválida: parámetros del motor de pivots";
      return false;
   }
   return true;
}

int ServerDayId()
{
   datetime now=TimeTradeServer();
   if(now<=0) return 0;
   MqlDateTime dt;
   if(!TimeToStruct(now,dt)) return 0;
   return dt.year*10000+dt.mon*100+dt.day;
}

bool RefreshPersistentSafetyState()
{
   int day=ServerDayId();
   double equity=AccountInfoDouble(ACCOUNT_EQUITY);
   if(day<=0 || equity<=0.0) return false;

   string dayKey=AccountSafetyStateKey("day");
   string lossKey=AccountSafetyStateKey("loss");
   string peakKey=AccountSafetyStateKey("peak");
   double storedDay=0.0;
   bool hasStoredDay=ReadSafetyStateValue(dayKey,storedDay);
   bool sameDay=hasStoredDay && (int)MathRound(storedDay)==day;
   double accountProfit=0.0;
   if(!TodayAccountProfit(accountProfit)) return false;
   if(!sameDay)
   {
      if(!SetSafetyStateValue(dayKey,(double)day) || !SetSafetyStateValue(lossKey,0.0)) return false;
   }

   double storedLoss=0.0;
   if(sameDay && GlobalVariableCheck(lossKey) && !ReadSafetyStateValue(lossKey,storedLoss)) return false;
   double observedLoss=MathMax(0.0,-accountProfit);
   g_dailyLossUsed=MathMax(observedLoss,MathMax(0.0,storedLoss));
   if(!SetSafetyStateValue(lossKey,g_dailyLossUsed)) return false;

   double storedPeak=0.0;
   if(GlobalVariableCheck(peakKey) && !ReadSafetyStateValue(peakKey,storedPeak)) return false;
   g_peakEquity=MathMax(equity,MathMax(g_peakEquity,storedPeak));
   if(!SetSafetyStateValue(peakKey,g_peakEquity)) return false;
   return FlushSafetyState();
}

double RemainingDailyLossBudget()
{
   return MathMax(0.0,DailyLossLimitUSD-g_dailyLossUsed);
}

double EncodeEntryReservation(const datetime bar,const EntryReservationPhase phase)
{
   if(bar<=0 || phase==ENTRY_RESERVATION_NONE) return 0.0;
   return (double)bar*10.0+(double)phase;
}

bool DecodeEntryReservation(const double encoded,datetime &bar,EntryReservationPhase &phase)
{
   bar=0;
   phase=ENTRY_RESERVATION_NONE;
   long raw=(long)MathRound(encoded);
   if(raw==0) return true;
   if(raw<0) return false;
   int rawPhase=(int)(raw%10);
   long rawBar=raw/10;
   if(rawBar<=0 || (rawPhase!=(int)ENTRY_RESERVATION_PENDING && rawPhase!=(int)ENTRY_RESERVATION_CONFIRMED)) return false;
   bar=(datetime)rawBar;
   phase=(EntryReservationPhase)rawPhase;
   return true;
}

bool ReadEntryReservation(datetime &bar,EntryReservationPhase &phase,double &rawValue)
{
   bar=0;
   phase=ENTRY_RESERVATION_NONE;
   rawValue=0.0;
   string key=EntrySafetyStateKey();
   if(!IsSafetyStateKeyValid(key)) return false;
   if(!GlobalVariableCheck(key)) return true;
   if(!ReadSafetyStateValue(key,rawValue)) return false;
   return DecodeEntryReservation(rawValue,bar,phase);
}

bool EntryReservationForBar(const datetime bar,EntryReservationPhase &phase)
{
   phase=ENTRY_RESERVATION_NONE;
   datetime reservedBar=0;
   double rawValue=0.0;
   if(!ReadEntryReservation(reservedBar,phase,rawValue)) return false;
   if(reservedBar!=bar) phase=ENTRY_RESERVATION_NONE;
   return true;
}

bool InitializeEntryReservationState()
{
   string key=EntrySafetyStateKey();
   if(!IsSafetyStateKeyValid(key)) return false;
   if(!GlobalVariableCheck(key) && !SetSafetyStateValue(key,0.0)) return false;
   datetime reservedBar=0;
   EntryReservationPhase phase=ENTRY_RESERVATION_NONE;
   double rawValue=0.0;
   return ReadEntryReservation(reservedBar,phase,rawValue);
}

bool SetEntryReservationPhase(const datetime bar,const EntryReservationPhase expected,const EntryReservationPhase next)
{
   if(bar<=0 || expected==ENTRY_RESERVATION_NONE) return false;
   string key=EntrySafetyStateKey();
   if(!IsSafetyStateKeyValid(key)) return false;
   datetime reservedBar=0;
   EntryReservationPhase actual=ENTRY_RESERVATION_NONE;
   double prior=0.0;
   if(!ReadEntryReservation(reservedBar,actual,prior)) return false;
   if(reservedBar!=bar || actual!=expected) return false;
   double replacement=EncodeEntryReservation(bar,next);
   ResetLastError();
   if(!GlobalVariableSetOnCondition(key,replacement,prior) || GetLastError()!=0) return false;
   return FlushSafetyState();
}

bool ReserveH1EntryPending(const datetime bar)
{
   if(bar<=0) return false;
   string key=EntrySafetyStateKey();
   // The key is created only during OnInit. Refuse to recreate it mid-run: that
   // would make a deleted/unknown reservation indistinguishable from no entry.
   if(!IsSafetyStateKeyValid(key) || !GlobalVariableCheck(key)) return false;
   datetime reservedBar=0;
   EntryReservationPhase phase=ENTRY_RESERVATION_NONE;
   double prior=0.0;
   if(!ReadEntryReservation(reservedBar,phase,prior)) return false;
   if(reservedBar==bar && phase!=ENTRY_RESERVATION_NONE) return false;
   double pending=EncodeEntryReservation(bar,ENTRY_RESERVATION_PENDING);
   ResetLastError();
   if(!GlobalVariableSetOnCondition(key,pending,prior) || GetLastError()!=0) return false;
   g_entryUsedThisBar=true;
   return FlushSafetyState();
}

bool ConfirmH1Entry(const datetime bar)
{
   return SetEntryReservationPhase(bar,ENTRY_RESERVATION_PENDING,ENTRY_RESERVATION_CONFIRMED);
}

bool ReleasePendingH1Entry(const datetime bar)
{
   return SetEntryReservationPhase(bar,ENTRY_RESERVATION_PENDING,ENTRY_RESERVATION_NONE);
}

bool RiskGuardsPass()
{
   string configMsg="";
   if(!SafetyInputsValid(configMsg))
   {
      g_lastDecision=configMsg;
      return false;
   }
   if(!RefreshPersistentSafetyState())
   {
      g_lastDecision="Bloqueado: no se pudo cargar el estado persistente de seguridad";
      return false;
   }

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(g_peakEquity > 0.0)
   {
      double dd = 100.0 * (g_peakEquity - equity) / g_peakEquity;
      if(dd >= MaxDrawdownPercent)
      {
         g_lastDecision = "Bloqueado: drawdown máximo alcanzado";
         return false;
      }
   }

   if(RemainingDailyLossBudget()<=0.0)
   {
      g_lastDecision = "Bloqueado: pérdida diaria máxima";
      return false;
   }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return false;
   double spread = tick.ask - tick.bid;
   if(spread > MaxSpreadUSD)
   {
      g_lastDecision = StringFormat("Bloqueado: spread %.2f > %.2f", spread, MaxSpreadUSD);
      return false;
   }

   return true;
}

bool HasHighImpactUSDNews()
{
   if(!UseNewsFilter) return false;
   datetime now = TimeTradeServer();
   datetime from = now - NewsBlockBeforeMin * 60;
   datetime to   = now + NewsBlockAfterMin * 60;
   MqlCalendarValue values[];
   int n = CalendarValueHistory(values, from, to, "US", "USD");
   if(n <= 0) return false;

   for(int i=0; i<n; i++)
   {
      {
         MqlCalendarEvent ev;
         if(CalendarEventById(values[i].event_id, ev))
         {
            if(ev.importance == CALENDAR_IMPORTANCE_HIGH)
            {
               if(values[i].time >= from && values[i].time <= to)
               {
                  g_lastDecision = "Bloqueado: noticia USD de alto impacto";
                  return true;
               }
            }
         }
      }
   }
   return false;
}

double PlannedRiskAmount()
{
   double equity=AccountInfoDouble(ACCOUNT_EQUITY);
   double effectiveRiskPercent=MathMax(0.0,MathMin(RiskPercent,HARD_MAX_RISK_PERCENT));
   return MathMax(0.0,equity*effectiveRiskPercent/100.0);
}

double WorstCaseFillPrice(const ENUM_ORDER_TYPE type,const double requestedPrice,const BrokerContractSpec &spec)
{
   double deviation=MAX_EXECUTION_DEVIATION_POINTS*_Point;
   if(type==ORDER_TYPE_BUY) return AlignPriceToTick(requestedPrice+deviation,spec,true);
   if(type==ORDER_TYPE_SELL) return AlignPriceToTick(requestedPrice-deviation,spec,false);
   return 0.0;
}

bool ExecutionResultConfirmed(const uint retcode,const ulong deal)
{
   return (retcode==TRADE_RETCODE_DONE || retcode==TRADE_RETCODE_DONE_PARTIAL) && deal>0;
}

// A PENDING reservation is released only when there is no possible fill. Timeout,
// connection and other ambiguous outcomes intentionally stay PENDING until H1 ends.
bool ExecutionFailureIsSafelyFinal(const bool requestSent,const uint retcode,const ulong deal)
{
   if(deal>0) return false;
   if(!requestSent) return true;
   switch(retcode)
   {
      case TRADE_RETCODE_REQUOTE:
      case TRADE_RETCODE_REJECT:
      case TRADE_RETCODE_CANCEL:
      case TRADE_RETCODE_INVALID:
      case TRADE_RETCODE_INVALID_VOLUME:
      case TRADE_RETCODE_INVALID_PRICE:
      case TRADE_RETCODE_INVALID_STOPS:
      case TRADE_RETCODE_TRADE_DISABLED:
      case TRADE_RETCODE_MARKET_CLOSED:
      case TRADE_RETCODE_NO_MONEY:
      case TRADE_RETCODE_PRICE_CHANGED:
      case TRADE_RETCODE_PRICE_OFF:
      case TRADE_RETCODE_INVALID_EXPIRATION:
      case TRADE_RETCODE_NO_CHANGES:
      case TRADE_RETCODE_SERVER_DISABLES_AT:
      case TRADE_RETCODE_CLIENT_DISABLES_AT:
      case TRADE_RETCODE_FROZEN:
      case TRADE_RETCODE_INVALID_FILL:
      case TRADE_RETCODE_ONLY_REAL:
      case TRADE_RETCODE_LIMIT_ORDERS:
      case TRADE_RETCODE_LIMIT_VOLUME:
      case TRADE_RETCODE_INVALID_ORDER:
      case TRADE_RETCODE_POSITION_CLOSED:
         return true;
   }
   return false;
}

bool PositionSizeForRisk(ENUM_ORDER_TYPE type,double priceOpen,double slPrice,double riskAmount,
                         const BrokerContractSpec &spec,double &lots)
{
   if(riskAmount<=0.0) return false;
   double referenceVolume=MathMax(spec.volumeMin,MathMin(1.0,spec.volumeMax));
   referenceVolume=NormalizeVolumeDown(referenceVolume,spec);
   if(referenceVolume<=0.0) return false;

   double referenceLoss=0.0;
   if(!OrderCalcProfit(type,_Symbol,referenceVolume,priceOpen,slPrice,referenceLoss)) return false;
   referenceLoss=MathAbs(referenceLoss)+RoundTurnCommissionPerLot*referenceVolume;
   if(referenceLoss<=0.0) return false;
   lots=NormalizeVolumeDown(riskAmount*referenceVolume/referenceLoss,spec);
   return lots>0.0;
}

bool RiskAtSL(ENUM_ORDER_TYPE type,double priceOpen,double slPrice,double lots,double &riskAmount)
{
   double p=0.0;
   if(!OrderCalcProfit(type,_Symbol,lots,priceOpen,slPrice,p)) return false;
   riskAmount=MathAbs(p)+RoundTurnCommissionPerLot*lots;
   return riskAmount>0.0;
}

bool MarginAllowsOrder(ENUM_ORDER_TYPE type,double price,double lots,
                       const BrokerContractSpec &spec,string &msg)
{
   msg="";
   double requiredMargin=0.0;
   ResetLastError();
   if(!OrderCalcMargin(type,_Symbol,lots,price,requiredMargin) || GetLastError()!=0 || requiredMargin<0.0)
   {
      msg="Bloqueado: no se pudo calcular el margen requerido";
      return false;
   }
   ResetLastError();
   double freeMargin=AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   if(GetLastError()!=0 || freeMargin<0.0)
   {
      msg="Bloqueado: margen libre no disponible";
      return false;
   }
   if(requiredMargin>freeMargin+1e-6)
   {
      msg=StringFormat("Bloqueado: margen requerido %.2f %s > libre %.2f %s",
         requiredMargin,spec.accountCurrency,freeMargin,spec.accountCurrency);
      return false;
   }
   return true;
}

bool BuildDynamicStop(int direction,double price,double atr,double &slPrice)
{
   if(atr<=0.0) return false;
   int hiIdx=iHighest(_Symbol,PERIOD_H1,MODE_HIGH,StructureLookback,2);
   int loIdx=iLowest(_Symbol,PERIOD_H1,MODE_LOW,StructureLookback,2);
   double recentHigh=hiIdx>=0?iHigh(_Symbol,PERIOD_H1,hiIdx):price;
   double recentLow=loIdx>=0?iLow(_Symbol,PERIOD_H1,loIdx):price;
   double minDist=MathAbs(MinStopATR)*atr;
   double baseDist=MathAbs(ATRStopMultiplier)*atr;
   double maxDist=MathAbs(MaxStopATR)*atr;
   if(minDist<=0.0) minDist=atr;
   if(baseDist<minDist) baseDist=minDist;
   if(maxDist<baseDist) maxDist=baseDist;
   double buffer=MathAbs(StructureBufferATR)*atr;
   double dist=baseDist;
   if(direction>0)
   {
      double structureDist=price-(recentLow-buffer);
      if(structureDist>dist) dist=structureDist;
      dist=MathMin(dist,maxDist);
      dist=MathMax(dist,minDist);
      slPrice=price-dist;
   }
   else
   {
      double structureDist=(recentHigh+buffer)-price;
      if(structureDist>dist) dist=structureDist;
      dist=MathMin(dist,maxDist);
      dist=MathMax(dist,minDist);
      slPrice=price+dist;
   }
   slPrice=NormalizeDouble(slPrice,_Digits);
   return true;
}

bool TPPriceForMoney(ENUM_ORDER_TYPE type,double priceOpen,double lots,double targetAmount,
                     const BrokerContractSpec &spec,double &tpPrice)
{
   if(spec.tickSize<=0.0 || targetAmount<=0.0 || lots<=0.0) return false;
   double estimatedCost=RoundTurnCommissionPerLot*lots;
   double lo,hi,mid,profit;
   if(type==ORDER_TYPE_BUY)
   {
      lo=priceOpen; hi=priceOpen+500.0;
      for(int k=0;k<50;k++)
      {
         mid=(lo+hi)/2.0; profit=0.0;
         if(!OrderCalcProfit(type,_Symbol,lots,priceOpen,mid,profit)) return false;
         if(profit-estimatedCost>=targetAmount) hi=mid; else lo=mid;
      }
      tpPrice=AlignPriceToTick(hi,spec,true);
   }
   else
   {
      lo=MathMax(spec.tickSize,priceOpen-500.0); hi=priceOpen;
      for(int k=0;k<50;k++)
      {
         mid=(lo+hi)/2.0; profit=0.0;
         if(!OrderCalcProfit(type,_Symbol,lots,priceOpen,mid,profit)) return false;
         if(profit-estimatedCost>=targetAmount) lo=mid; else hi=mid;
      }
      tpPrice=AlignPriceToTick(lo,spec,false);
   }
   if(tpPrice<=0.0 || !OrderCalcProfit(type,_Symbol,lots,priceOpen,tpPrice,profit)) return false;
   return profit-estimatedCost+1e-6>=targetAmount;
}

double RecentVolumeAverage(int startShift=2)
{
   double sum=0.0; int count=0;
   for(int i=startShift; i<startShift+VolumeLookback; i++)
   {
      long v=iVolume(_Symbol, PERIOD_H1, i);
      if(v>0){ sum+=(double)v; count++; }
   }
   return count>0 ? sum/count : 0.0;
}

string SetupName(int dir, bool breakout, bool pullback, bool momentum)
{
   if(breakout) return "BREAKOUT";
   if(pullback) return "PULLBACK";
   if(momentum) return "MOMENTUM";
   return dir>0 ? "CONTINUATION LONG" : "CONTINUATION SHORT";
}

string JsonFieldString(const string json,const string key,const string fallback="")
{
   string needle="\""+key+"\":";
   int p=StringFind(json,needle);
   if(p<0) return fallback;
   p+=StringLen(needle);
   while(p<StringLen(json) && (StringGetCharacter(json,p)==' ' || StringGetCharacter(json,p)=='\t')) p++;
   if(p>=StringLen(json) || StringGetCharacter(json,p)!='\"') return fallback;
   p++;
   int e=StringFind(json,"\"",p);
   if(e<0) return fallback;
   return StringSubstr(json,p,e-p);
}

double JsonFieldDouble(const string json,const string key,const double fallback=0.0)
{
   string needle="\""+key+"\":";
   int p=StringFind(json,needle);
   if(p<0) return fallback;
   p+=StringLen(needle);
   string tail=StringSubstr(json,p);
   int e=StringFind(tail,",");
   int e2=StringFind(tail,"}");
   if(e<0 || (e2>=0 && e2<e)) e=e2;
   if(e<0) e=StringLen(tail);
   return StringToDouble(StringSubstr(tail,0,e));
}

bool JsonFieldBool(const string json,const string key,const bool fallback=false)
{
   string needle="\""+key+"\":";
   int p=StringFind(json,needle);
   if(p<0) return fallback;
   p+=StringLen(needle);
   string tail=StringSubstr(json,p,5);
   if(StringFind(tail,"true")==0) return true;
   if(StringFind(tail,"false")==0) return false;
   return fallback;
}

bool LoadWorldNews()
{
   g_newsBias=0; g_newsConfidence=0; g_newsRisk="UNKNOWN"; g_newsDirection="NEUTRO"; g_newsSummary="Sin análisis de noticias."; g_newsCount=0; g_newsUpdated=""; g_newsUpdatedEpoch=0; g_newsAvailable=false;
   int h=FileOpen("gold_news_analysis.json",FILE_COMMON|FILE_READ|FILE_TXT|FILE_ANSI);
   if(h==INVALID_HANDLE) return false;
   string json="";
   while(!FileIsEnding(h)) json+=FileReadString(h)+"\n";
   FileClose(h);
   g_newsUpdated=JsonFieldString(json,"updated_at","");
   g_newsUpdatedEpoch=(long)MathRound(JsonFieldDouble(json,"updated_epoch",0.0));
   g_newsBias=(int)MathRound(JsonFieldDouble(json,"bias",0.0));
   g_newsConfidence=(int)MathRound(JsonFieldDouble(json,"confidence",0.0));
   g_newsRisk=JsonFieldString(json,"risk","UNKNOWN");
   g_newsDirection=JsonFieldString(json,"direction","NEUTRO");
   g_newsSummary=JsonFieldString(json,"summary","Sin resumen.");
   g_newsCount=(int)MathRound(JsonFieldDouble(json,"article_count",0.0));
   g_newsAvailable=JsonFieldBool(json,"available",false);
   if(g_newsUpdated=="") return false;
   if(!g_newsAvailable) return false;
   return true;
}

bool NewsDataFresh()
{
   if(!UseWorldNewsAnalysis) return true;
   if(g_newsUpdatedEpoch<=0) return false;
   long age=(long)TimeGMT()-g_newsUpdatedEpoch;
   return age>=0 && age<=MaxNewsAgeMinutes*60;
}

int NewsAlignmentPoints(const int direction)
{
   if(!UseWorldNewsAnalysis || g_newsConfidence<50) return 0;
   double magnitude=MathMin(100.0,MathAbs((double)g_newsBias));
   if(magnitude<10.0) return 0;
   int pts=(int)MathRound((magnitude/100.0)*NewsScoreMaxPoints);
   pts=(int)MathMax(1,MathMin(NewsScoreMaxPoints,pts));
   if(direction>0) return g_newsBias>0 ? pts : (g_newsBias<0 ? -pts : 0);
   return g_newsBias<0 ? pts : (g_newsBias>0 ? -pts : 0);
}

bool WorldNewsPass(string &msg)
{
   msg="";
   if(!UseWorldNewsAnalysis) return true;
   if(!LoadWorldNews() || !g_newsAvailable)
   {
      msg="Bloqueado: no hay análisis mundial de noticias disponible";
      return !RequireFreshWorldNews;
   }
   if(!NewsDataFresh())
   {
      msg=StringFormat("Bloqueado: noticias desactualizadas (última actualización %s)",g_newsUpdated);
      return !RequireFreshWorldNews;
   }
   if(BlockHighNewsRisk && g_newsRisk=="HIGH")
   {
      msg="Bloqueado: riesgo de noticias mundiales HIGH";
      return false;
   }
   return true;
}

bool BuildSignal(int &direction, int &score, string &setup, string &reason, double &targetR)
{
   direction=0; score=0; setup="-"; reason=""; targetR=ChillTargetR;
   g_diagBlockReason=""; g_diagTechnicalReason=""; g_diagNewsReason="";

   double emaF, emaS, h4F, h4S, rsi, adx, atr;
   if(!GetValue(hEmaFast,0,1,emaF) || !GetValue(hEmaSlow,0,1,emaS) ||
      !GetValue(hEmaHTFFast,0,1,h4F) || !GetValue(hEmaHTFSlow,0,1,h4S) ||
      !GetValue(hRSI,0,1,rsi) || !GetValue(hADX,0,1,adx) || !GetValue(hATR,0,1,atr))
   {
      reason="Bloqueado: no se pudieron leer todos los indicadores H1/H4";
      g_diagBlockReason=reason;
      return false;
   }

   double close1=iClose(_Symbol,PERIOD_H1,1), open1=iOpen(_Symbol,PERIOD_H1,1);
   double high2=iHigh(_Symbol,PERIOD_H1,2), low2=iLow(_Symbol,PERIOD_H1,2);
   int hiIdx=iHighest(_Symbol,PERIOD_H1,MODE_HIGH,StructureLookback,2);
   int loIdx=iLowest(_Symbol,PERIOD_H1,MODE_LOW,StructureLookback,2);
   double recentHigh=hiIdx>=0?iHigh(_Symbol,PERIOD_H1,hiIdx):high2;
   double recentLow=loIdx>=0?iLow(_Symbol,PERIOD_H1,loIdx):low2;
   double avgVol=RecentVolumeAverage(2);
   double curVol=(double)iVolume(_Symbol,PERIOD_H1,1);

   bool bullHTF = h4F > h4S;
   bool bearHTF = h4F < h4S;
   bool bullLTF = emaF > emaS;
   bool bearLTF = emaF < emaS;
   bool strongTrend = adx >= MinADX;
   bool adxBuild = adx >= 16.0;
   bool volOK = (avgVol>0.0 && curVol >= avgVol*1.05);

   GoldScoutPivotConfig pivotConfig;
   ConfigurePivotEngine(pivotConfig);
   GoldScoutPivot confirmedPivots[];
   GoldScoutStructureClassification pivotStructure;
   GS_ClearStructureClassification(pivotStructure);
   bool pivotDataAvailable=GS_LoadConfirmedPivots(_Symbol,PERIOD_H1,hATR,PivotLookbackBars,pivotConfig,confirmedPivots);
   bool pivotStructureValid=pivotDataAvailable &&
      GS_ClassifyConfirmedStructure(confirmedPivots,pivotConfig.toleranceAtr,_Point,pivotStructure);
   if(!pivotStructureValid) GS_ClearStructureClassification(pivotStructure);
   bool hh=pivotStructure.hh;
   bool hl=pivotStructure.hl;
   bool lh=pivotStructure.lh;
   bool ll=pivotStructure.ll;
   string pivotStructureName=GS_StructureStateName(pivotStructure.state);
   RefreshStructurePatternDiagnostics(confirmedPivots,pivotDataAvailable);
   RefreshContinuationPatternDiagnostics(confirmedPivots,pivotDataAvailable);

   bool breakLong = close1 > recentHigh;
   bool breakShort = close1 < recentLow;
   bool momLong = close1 > high2;
   bool momShort = close1 < low2;

   // Pullback detection now recognizes a correction on H1 inside an H4 trend.
   bool nearFast = atr>0.0 && MathAbs(close1-emaF) <= atr*0.75;
   bool pullLong = atr>0.0 && nearFast && close1 <= emaF && rsi<=48.0 && (bullHTF || bullLTF) && hl;
   bool pullShort = atr>0.0 && nearFast && close1 >= emaF && rsi>=52.0 && (bearHTF || bearLTF) && lh;

   // A reversal-style pullback can be valid even when H1 temporarily disagrees with H4.
   bool htfPullLong = bullHTF && !bullLTF && nearFast && rsi<=42.0 && hl && close1>=low2;
   bool htfPullShort = bearHTF && !bearLTF && nearFast && rsi>=58.0 && lh && close1<=high2;
   pullLong = pullLong || htfPullLong;
   pullShort = pullShort || htfPullShort;

   int longStructuralPoints=GS_StructuralBucketPoints(pivotStructure.state,1,pullLong,breakLong,momLong);
   int shortStructuralPoints=GS_StructuralBucketPoints(pivotStructure.state,-1,pullShort,breakShort,momShort);
   bool clearLong = bullHTF && (bullLTF || htfPullLong) && adx>=25.0 && ((rsi>=50.0 && rsi<=68.0) || htfPullLong) && (hh || hl || breakLong || pullLong);
   bool clearShort = bearHTF && (bearLTF || htfPullShort) && adx>=25.0 && ((rsi>=32.0 && rsi<=50.0) || htfPullShort) && (ll || lh || breakShort || pullShort);

   // Directional scoring: trend strength only rewards the side it actually supports.
   int longScore=0, shortScore=0;
   if(bullHTF) longScore+=20;
   if(bearHTF) shortScore+=20;
   if(bullLTF) longScore+=15;
   if(bearLTF) shortScore+=15;
   if(bullHTF && bullLTF) longScore+=5;
   if(bearHTF && bearLTF) shortScore+=5;

   if(adxBuild) { if(bullHTF || bullLTF) longScore+=10; if(bearHTF || bearLTF) shortScore+=10; }

   if(rsi>=50.0 && rsi<=68.0) longScore+=10;
   if(rsi>=32.0 && rsi<=50.0) shortScore+=10;
   // H4->H1 pullback confirmation is accounted for only inside the structural bucket.

   longScore+=longStructuralPoints;
   shortScore+=shortStructuralPoints;
   if(volOK) { if(close1>=open1) longScore+=5; else shortScore+=5; }

   longScore=(int)MathMin(100,longScore);
   shortScore=(int)MathMin(100,shortScore);

   g_diagEmaFast=emaF; g_diagEmaSlow=emaS; g_diagH4Fast=h4F; g_diagH4Slow=h4S;
   g_diagRSI=rsi; g_diagADX=adx; g_diagATR=atr; g_diagCurVol=curVol; g_diagAvgVol=avgVol;
   g_diagLongTech=longScore; g_diagShortTech=shortScore;
   g_diagH4Trend=bullHTF?"ALCISTA":(bearHTF?"BAJISTA":"NEUTRA");
   g_diagH1Trend=bullLTF?"ALCISTA":(bearLTF?"BAJISTA":"NEUTRA");
   g_diagVolume=volOK?"OK":"DEBIL";

   if(breakLong || breakShort) g_diagStructure="BREAKOUT";
   else if(htfPullLong || htfPullShort) g_diagStructure="PULLBACK H4->H1";
   else if(pullLong || pullShort) g_diagStructure="PULLBACK";
   else if(hh || hl || lh || ll) g_diagStructure="ESTRUCTURA "+pivotStructureName;
   else if(momLong || momShort) g_diagStructure="MOMENTUM";
   else if(strongTrend) g_diagStructure="CONTINUACION";
   else g_diagStructure="RANGO/NEUTRA";

   int newsPtsLong=NewsAlignmentPoints(1);
   int newsPtsShort=NewsAlignmentPoints(-1);
   g_diagLongNewsPts=newsPtsLong; g_diagShortNewsPts=newsPtsShort;
   // Intrabar confirmation is additive and is recalculated while the current H1 candle evolves.
   int finalLong=(int)MathMax(0,MathMin(100,longScore+newsPtsLong+g_intrabarLongBoost));
   int finalShort=(int)MathMax(0,MathMin(100,shortScore+newsPtsShort+g_intrabarShortBoost));
   g_diagLongFinal=finalLong; g_diagShortFinal=finalShort;

   g_diagTechnicalReason=StringFormat("Tecnico L=%d/S=%d | H4=%s | H1=%s | RSI=%.1f | ADX=%.1f | ATR=%.2f | volumen=%s | estructura=%s | pivots=%d estado=%s bucketL=%d bucketS=%d | HH=%s HL=%s LH=%s LL=%s",
      longScore,shortScore,g_diagH4Trend,g_diagH1Trend,rsi,adx,atr,g_diagVolume,g_diagStructure,
      pivotStructure.alternatingPivotCount,pivotStructureName,longStructuralPoints,shortStructuralPoints,
      hh?"SI":"NO",hl?"SI":"NO",lh?"SI":"NO",ll?"SI":"NO");
   g_diagNewsReason=StringFormat("Noticias: bias=%d conf=%d riesgo=%s | puntos L=%+d/S=%+d | dir=%s | %s",
      g_newsBias,g_newsConfidence,g_newsRisk,newsPtsLong,newsPtsShort,g_newsDirection,g_newsSummary);

   int bestScore=MathMax(finalLong,finalShort);
   if(bestScore < ArmScoreThreshold)
   {
      score=bestScore;
      direction=0;
      g_diagBlockReason=StringFormat("NO TRADE | score final L=%d / S=%d (< arm %d)",finalLong,finalShort,ArmScoreThreshold);
      reason=StringFormat("%s | %s | %s",g_diagBlockReason,g_diagTechnicalReason,g_diagNewsReason);
      return false;
   }

   direction=(finalLong>=finalShort?1:-1);
   score=(direction>0?finalLong:finalShort);
   bool isBreak=(direction>0?breakLong:breakShort);
   bool isPull=(direction>0?pullLong:pullShort);
   bool isMom=(direction>0?momLong:momShort);
   setup=SetupName(direction,isBreak,isPull,isMom);

   bool clear=(direction>0?clearLong:clearShort);
   targetR=clear ? MathMax(GodTargetR,MinRewardRisk) : MathMax(ChillTargetR,MinRewardRisk);
   g_diagTechnicalReason += StringFormat(" | candidato=%s score=%d setup=%s | claro=%s",direction>0?"LONG":"SHORT",score,setup,clear?"SI":"NO");

   reason=StringFormat("%s | score %d/100 | tech L=%d/S=%d | final L=%d/S=%d | H4 %s | H1 %s | RSI %.1f | ADX %.1f | ATR %.2f | volumen %s | estructura %s | noticias %s bias=%d conf=%d risk=%s | objetivo %.2fR",
      direction>0?"LONG":"SHORT",score,longScore,shortScore,finalLong,finalShort,
      g_diagH4Trend,g_diagH1Trend,rsi,adx,atr,g_diagVolume,setup,
      g_newsDirection,g_newsBias,g_newsConfidence,g_newsRisk,targetR);
   return true;
}

int IntrabarConfirmationBoost(const int direction)
{
   if(!UseIntrabarMonitoring || direction==0) return 0;
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol,tick)) return 0;
   double atr=0.0, ema=0.0, rsi=0.0, adx=0.0;
   if(!GetValue(hATR,0,1,atr) || atr<=0.0) return 0;
   bool haveEma=GetValue(hEmaFast,0,0,ema);
   bool haveRsi=GetValue(hRSI,0,0,rsi);
   bool haveAdx=GetValue(hADX,0,0,adx);
   double price=direction>0?tick.ask:tick.bid;
   double prevHigh=iHigh(_Symbol,PERIOD_H1,1);
   double prevLow=iLow(_Symbol,PERIOD_H1,1);
   double buffer=MathMax(0.05*atr,2.0*_Point);
   int boost=0;
   bool breakout=direction>0 ? price>prevHigh+buffer : price<prevLow-buffer;
   if(breakout) boost+=10;
   if(haveEma)
   {
      if(direction>0 && price>ema) boost+=5;
      if(direction<0 && price<ema) boost+=5;
   }
   if(haveRsi)
   {
      if(direction>0 && rsi>=50.0) boost+=5;
      if(direction<0 && rsi<=50.0) boost+=5;
   }
   if(haveAdx && adx>=25.0) boost+=3;
   return boost;
}

bool IntrabarTrigger(const int direction,const string setup)
{
   if(direction==0) return false;
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol,tick)) return false;
   double atr=0.0, ema=0.0, rsi=0.0;
   if(!GetValue(hATR,0,1,atr) || atr<=0.0) return false;
   bool haveEma=GetValue(hEmaFast,0,0,ema);
   bool haveRsi=GetValue(hRSI,0,0,rsi);
   double price=direction>0?tick.ask:tick.bid;
   double prevHigh=iHigh(_Symbol,PERIOD_H1,1);
   double prevLow=iLow(_Symbol,PERIOD_H1,1);
   double buffer=MathMax(0.05*atr,2.0*_Point);
   bool breakout=direction>0 ? price>prevHigh+buffer : price<prevLow-buffer;
   bool emaRsiConfirm=false;
   if(haveEma && haveRsi)
      emaRsiConfirm=(direction>0 ? (price>ema && rsi>=50.0) : (price<ema && rsi<=50.0));
   bool pullbackRecovery=false;
   if(haveEma && haveRsi)
   {
      pullbackRecovery=(direction>0 ? (g_diagH4Trend=="ALCISTA" && price>ema && rsi>=45.0)
                                   : (g_diagH4Trend=="BAJISTA" && price<ema && rsi<=55.0));
   }
   if(breakout || emaRsiConfirm || pullbackRecovery) return true;
   return false;
}

void ResetIntrabarPlan(const datetime bar)
{
   g_armed=false;
   EntryReservationPhase reservation=ENTRY_RESERVATION_NONE;
   // A corrupt or unreadable reservation is treated as used until CanOpenTrade
   // reports the persistent-state error and fails closed.
   g_entryUsedThisBar=!EntryReservationForBar(bar,reservation) || reservation!=ENTRY_RESERVATION_NONE;
   g_armedDirection=0;
   g_armedScore=0;
   g_armedSetup="-";
   g_monitorState="ESPERANDO";
   g_monitorReason="Analizando contexto H1...";
   g_monitorAgeSec=0;
   g_intrabarLongBoost=0;
   g_intrabarShortBoost=0;
}

void ArmIntrabarPlan(const int direction,const int score,const string setup,const string reason)
{
   g_armed=true;
   g_armedDirection=direction;
   g_armedScore=score;
   g_armedSetup=setup;
   g_monitorState="ARMADO";
   g_monitorReason=reason;
   g_monitorAgeSec=0;
}

void LogIntrabarHeartbeat(const bool candidate,const int direction,const int score)
{
   if(!DebugIntrabarLogs) return;
   datetime now=TimeTradeServer();
   if(now<=0) now=TimeLocal();
   if(now<=0) return;
   if(g_lastIntrabarHeartbeat>0 && (long)(now-g_lastIntrabarHeartbeat)<INTRABAR_DIAGNOSTIC_INTERVAL_SECONDS) return;
   g_lastIntrabarHeartbeat=now;
   string candidateLabel=!candidate ? "NONE" : (direction>0 ? "LONG" : (direction<0 ? "SHORT" : "UNKNOWN"));
   PrintFormat("[GoldScout][INTRABAR] alive | candidate=%s | score=%d | L=%d | S=%d | state=%s",
      candidateLabel,score,g_diagLongFinal,g_diagShortFinal,g_monitorState);
}

void MonitorIntrabar()
{
   if(!UseIntrabarMonitoring || g_entryUsedThisBar)
   {
      LogIntrabarHeartbeat(false,0,g_lastScore);
      UpdateDashboard();
      return;
   }

   datetime now=TimeTradeServer();
   g_monitorAgeSec=(long)(now-g_diagBar);
   LoadWorldNews();

   // Keep watching the current H1 bar even when there was no valid setup at startup.
   // A setup may form later in the hour; only then do we arm and wait for a trigger.
   g_intrabarLongBoost=IntrabarConfirmationBoost(1);
   g_intrabarShortBoost=IntrabarConfirmationBoost(-1);

   int direction=0,score=0; string setup="-",reason=""; double targetR=ChillTargetR;
   bool candidate=BuildSignal(direction,score,setup,reason,targetR);
   if(candidate && direction!=0)
   {
      g_armed=true;
      g_armedDirection=direction;
      g_armedScore=score;
      g_armedSetup=setup;
      g_monitorReason=reason;
   }

   if(!g_armed)
   {
      g_lastScore=MathMax(g_diagLongFinal,g_diagShortFinal);
      g_lastSetup="-";
      g_lastDirection="-";
      g_monitorState="ESPERANDO";
      g_monitorReason="Sin setup armado; vigilando continuamente por si aparece una oportunidad durante esta H1.";
      g_lastDecision="MONITOREANDO H1 | esperando condiciones suficientes";
      LogIntrabarHeartbeat(candidate,direction,g_lastScore);
      UpdateDashboard();
      return;
   }

   bool trigger=IntrabarTrigger(g_armedDirection,g_armedSetup);
   int liveBoost=(g_armedDirection>0?g_intrabarLongBoost:g_intrabarShortBoost);
   int effectiveScore=(candidate && direction==g_armedDirection) ? score : MathMin(100,g_armedScore+liveBoost);

   g_lastScore=effectiveScore;
   g_lastSetup=g_armedSetup;
   g_lastDirection=g_armedDirection>0?"LONG":(g_armedDirection<0?"SHORT":"-");

   if(trigger && effectiveScore>=MinScoreToTrade)
   {
      g_monitorState="TRIGGER CONFIRMADO";
      g_monitorReason=StringFormat("Trigger intrabar confirmado | base=%d + boost=%d = %d",g_armedScore,liveBoost,effectiveScore);
      TryTrade();
      if(g_lastDecision!="" && (StringFind(g_lastDecision,"SEÑAL SIMULADA")>=0 || StringFind(g_lastDecision,"ORDEN ENVIADA")>=0))
      {
         g_entryUsedThisBar=true;
         g_armed=false;
         g_monitorState="EN OPERACION";
         g_monitorReason="Entrada ejecutada; no se permiten nuevas entradas en esta H1.";
      }
      LogIntrabarHeartbeat(candidate,direction,g_lastScore);
      return;
   }

   if(effectiveScore>=MinScoreToTrade)
   {
      g_monitorState="ESPERANDO TRIGGER";
      g_monitorReason=StringFormat("Score suficiente %d, esperando confirmación de precio/estructura",effectiveScore);
   }
   else
   {
      g_monitorState="ARMADO";
      g_monitorReason=StringFormat("Score %d/%d | trigger=%s | boost=%d",effectiveScore,MinScoreToTrade,trigger?"SI":"NO",liveBoost);
   }
   g_lastDecision=StringFormat("MONITOREANDO %s | score=%d | %s",g_lastDirection,g_lastScore,g_monitorState);
   LogIntrabarHeartbeat(candidate,direction,effectiveScore);
   UpdateDashboard();
}

bool CanOpenTrade()
{
   datetime bar=iTime(_Symbol,PERIOD_H1,0);
   if(bar<=0)
   {
      g_lastDecision="Bloqueado: no se pudo identificar la vela H1 actual";
      g_diagBlockReason=g_lastDecision;
      return false;
   }
   EntryReservationPhase reservation=ENTRY_RESERVATION_NONE;
   if(!EntryReservationForBar(bar,reservation))
   {
      g_entryUsedThisBar=true;
      g_lastDecision="Bloqueado: no se pudo leer la reserva H1 persistente";
      g_diagBlockReason=g_lastDecision;
      return false;
   }
   if(g_entryUsedThisBar || reservation!=ENTRY_RESERVATION_NONE)
   {
      g_entryUsedThisBar=true;
      g_lastDecision=reservation==ENTRY_RESERVATION_PENDING
         ? "Bloqueado: reserva H1 PENDING; posible orden previa sin confirmar"
         : "Bloqueado: entrada H1 ya CONFIRMED";
      g_diagBlockReason=g_lastDecision;
      return false;
   }
   string positionBlock="";
   if(!PositionStateAllowsEntry(positionBlock))
   {
      g_lastDecision=positionBlock;
      g_diagBlockReason=g_lastDecision;
      return false;
   }
   if(!RiskGuardsPass())
   {
      g_diagBlockReason=g_lastDecision;
      return false;
   }
   return true;
}

void AppendDealLog(string direction, int score, string setup, double targetAmount, double lots, double sl, double tp, string reason)
{
   int h=FileOpen("xau_goldscout_trade_log.csv", FILE_COMMON|FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI, ';');
   if(h==INVALID_HANDLE) return;
   FileSeek(h,0,SEEK_END);
   if(FileTell(h)==0) FileWrite(h,"time","symbol","direction","score","setup","targetR","target_amount","lots","sl","tp","reason");
   FileWrite(h,TimeToString(TimeTradeServer(),TIME_DATE|TIME_SECONDS),_Symbol,direction,score,setup,"",DoubleToString(targetAmount,2),DoubleToString(lots,2),DoubleToString(sl,_Digits),DoubleToString(tp,_Digits),reason);
   FileClose(h);
}

void TryTrade()
{
   // Always build the technical/news diagnostic first, even when a guard later blocks execution.
   LoadWorldNews();
   int direction,score; string setup,reason; double targetR;
   bool signal=BuildSignal(direction,score,setup,reason,targetR);

   g_lastScore=score;
   g_lastSetup=setup;
   g_lastDirection=direction>0?"LONG":(direction<0?"SHORT":"-");

   Print("[GoldScout] ================= H1 ANALISIS =================");
   PrintFormat("[GoldScout] Bar=%s | H4=%s | H1=%s",TimeToString(g_diagBar,TIME_DATE|TIME_MINUTES),g_diagH4Trend,g_diagH1Trend);
   PrintFormat("[GoldScout] EMA H1=%.2f/%.2f | EMA H4=%.2f/%.2f | RSI=%.1f | ADX=%.1f | ATR=%.2f",
      g_diagEmaFast,g_diagEmaSlow,g_diagH4Fast,g_diagH4Slow,g_diagRSI,g_diagADX,g_diagATR);
   PrintFormat("[GoldScout] Volumen=%s (actual=%.0f promedio=%.0f) | Estructura=%s",g_diagVolume,g_diagCurVol,g_diagAvgVol,g_diagStructure);
   PrintFormat("[GoldScout] TECH SCORE L=%d / S=%d | NEWS pts L=%+d / S=%+d | FINAL L=%d / S=%d",
      g_diagLongTech,g_diagShortTech,g_diagLongNewsPts,g_diagShortNewsPts,g_diagLongFinal,g_diagShortFinal);
   PrintFormat("[GoldScout] NEWS bias=%d conf=%d%% risk=%s dir=%s | %s",g_newsBias,g_newsConfidence,g_newsRisk,g_newsDirection,g_newsSummary);

   if(!signal)
   {
      g_lastDecision=reason;
      g_diagBlockReason=reason;
      g_monitorState="SIN SETUP";
      g_monitorReason=reason;
      PrintFormat("[GoldScout] DECISION=NO TRADE | %s",reason);
      UpdateDashboard();
      return;
   }

   // During the H1 bar we first arm the candidate and wait for an intrabar trigger.
   if(score < MinScoreToTrade)
   {
      ArmIntrabarPlan(direction,score,setup,StringFormat("Candidato %s armado | score %d/%d | esperando trigger",direction>0?"LONG":"SHORT",score,MinScoreToTrade));
      g_lastDecision=StringFormat("ARMADO %s | score=%d/%d",direction>0?"LONG":"SHORT",score,MinScoreToTrade);
      g_lastScore=score; g_lastSetup=setup; g_lastDirection=direction>0?"LONG":"SHORT";
      PrintFormat("[GoldScout] ARMADO %s | setup=%s score=%d/%d | esperando trigger intrabar",g_lastDirection,setup,score,MinScoreToTrade);
      UpdateDashboard();
      return;
   }

   if(UseIntrabarMonitoring && !IntrabarTrigger(direction,setup))
   {
      ArmIntrabarPlan(direction,score,setup,"Score listo; esperando trigger intrabar de precio/estructura.");
      g_lastDecision=StringFormat("ESPERANDO TRIGGER %s | score=%d",direction>0?"LONG":"SHORT",score);
      PrintFormat("[GoldScout] ESPERANDO TRIGGER %s | setup=%s score=%d",g_lastDirection,setup,score);
      UpdateDashboard();
      return;
   }

   // Keep the plan armed until the entry is actually accepted.

   // Risk / position / broker guards after the analytical decision is known.
   if(!CanOpenTrade())
   {
      PrintFormat("[GoldScout] DECISION=BLOCKED | %s",g_diagBlockReason);
      UpdateDashboard();
      return;
   }

   if(HasHighImpactUSDNews())
   {
      g_diagBlockReason=g_lastDecision;
      PrintFormat("[GoldScout] DECISION=BLOCKED | %s",g_diagBlockReason);
      UpdateDashboard();
      return;
   }

   string newsMsg="";
   if(!WorldNewsPass(newsMsg))
   {
      g_lastDecision=newsMsg;
      g_diagBlockReason=newsMsg;
      PrintFormat("[GoldScout] DECISION=BLOCKED | %s",newsMsg);
      UpdateDashboard();
      return;
   }

   MqlTick tick; if(!SymbolInfoTick(_Symbol,tick))
   {
      g_lastDecision="Bloqueado: no se pudo leer precio de mercado";
      g_diagBlockReason=g_lastDecision;
      UpdateDashboard(); return;
   }
   ENUM_ORDER_TYPE type=(direction>0?ORDER_TYPE_BUY:ORDER_TYPE_SELL);
   double price=(direction>0?tick.ask:tick.bid);
   BrokerContractSpec contract;
   string contractMsg="";
   if(!LoadBrokerContract(contract,contractMsg))
   {
      g_lastDecision="Bloqueado: "+contractMsg;
      g_diagBlockReason=g_lastDecision;
      UpdateDashboard(); return;
   }
   double atr=0.0;
   if(!GetValue(hATR,0,1,atr) || !BuildDynamicStop(direction,price,atr,g_tempSL))
   {
      g_lastDecision="Bloqueado: no se pudo construir SL dinamico";
      g_diagBlockReason=g_lastDecision;
      UpdateDashboard(); return;
   }
   double sl=g_tempSL;
   // stops_level governs initial protection. freeze_level is validated and
   // reported, but applies to later trade modifications rather than widening
   // the strategy stop before a market entry.
   double minDist=contract.stopsLevel*contract.point;
   double stopReference=(direction>0?tick.bid:tick.ask);
   if(direction>0) sl=MathMin(sl,stopReference-minDist); else sl=MathMax(sl,stopReference+minDist);
   sl=AlignPriceToTick(sl,contract,direction<0);
   if(sl<=0.0 || (direction>0 && stopReference-sl+1e-9<minDist) ||
      (direction<0 && sl-stopReference+1e-9<minDist))
   {
      g_lastDecision="Bloqueado: no se pudo alinear el SL al tick/stops_level del broker";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   double remainingDailyBudget=RemainingDailyLossBudget();
   if(remainingDailyBudget<=0.0)
   {
      g_lastDecision="Bloqueado: presupuesto de pérdida diaria agotado";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }
   double plannedRisk=MathMin(PlannedRiskAmount(),remainingDailyBudget),lots=0.0;
   if(plannedRisk<=0.0)
   {
      g_lastDecision="Bloqueado: riesgo planificado no válido";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }
   // Size against the worst entry price permitted by the existing 30-point
   // deviation policy. This keeps planned price-to-SL loss inside the hard cap.
   double worstCasePrice=WorstCaseFillPrice(type,price,contract);
   if(worstCasePrice<=0.0)
   {
      g_lastDecision="Bloqueado: precio de peor ejecución no válido";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }
   if(!PositionSizeForRisk(type,worstCasePrice,sl,plannedRisk,contract,lots))
   {
      g_lastDecision=StringFormat("Bloqueado: lote mínimo/step del broker impide arriesgar %.2f %s",plannedRisk,contract.accountCurrency);
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   double actualRisk=0.0;
   if(!RiskAtSL(type,worstCasePrice,sl,lots,actualRisk))
   {
      g_lastDecision="Bloqueado: no se pudo calcular riesgo al SL";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }
   if(actualRisk>plannedRisk+1e-6 || actualRisk>remainingDailyBudget+1e-6)
   {
      g_lastDecision="Bloqueado: riesgo real al SL excede el presupuesto de seguridad";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   string marginMsg="";
   if(!MarginAllowsOrder(type,price,lots,contract,marginMsg))
   {
      g_lastDecision=marginMsg;
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   double targetAmount=actualRisk*targetR;
   double tp=0.0;
   if(!TPPriceForMoney(type,price,lots,targetAmount,contract,tp))
   {
      g_lastDecision="Bloqueado: no se pudo calcular TP";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }
   if(direction>0) tp=MathMax(tp,tick.bid+minDist); else tp=MathMin(tp,tick.ask-minDist);
   tp=AlignPriceToTick(tp,contract,direction>0);
   if(tp<=0.0 || (direction>0 && tp-tick.bid+1e-9<minDist) ||
      (direction<0 && tick.ask-tp+1e-9<minDist))
   {
      g_lastDecision="Bloqueado: no se pudo alinear el TP al tick/stops_level del broker";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   double slDist=MathAbs(price-sl),tpDist=MathAbs(tp-price);
   double rr=slDist>0.0?tpDist/slDist:0.0;
   if(rr+1e-9<MinRewardRisk)
   {
      g_lastDecision=StringFormat("Bloqueado: R:R %.2f < minimo %.2f",rr,MinRewardRisk);
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }
   string tag=(targetR>=GodTargetR-1e-9?"TRADE GOD":"CHILL");
   string comment=StringFormat("GOLDscout|%s|S%d|%s|R%.2f|risk%.2f|RR%.2f",direction>0?"BUY":"SELL",score,setup,targetR,actualRisk,rr);
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MAX_EXECUTION_DEVIATION_POINTS);

   // Recheck immediately before reserving/sending to narrow the window in
   // which another EA, a manual action or an earlier pending order can appear.
   string positionBlock="";
   if(!PositionStateAllowsEntry(positionBlock))
   {
      g_lastDecision=positionBlock;
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   datetime entryBar=iTime(_Symbol,PERIOD_H1,0);
   if(!ReserveH1EntryPending(entryBar))
   {
      g_lastDecision="Bloqueado: no se pudo crear reserva H1 PENDING persistente";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   bool requestSent=false;
   bool executionConfirmed=false;
   uint retcode=0;
   ulong deal=0;
   if(EnableLiveTrading)
   {
      requestSent=trade.PositionOpen(_Symbol,type,lots,price,sl,tp,comment);
      retcode=trade.ResultRetcode();
      deal=trade.ResultDeal();
      executionConfirmed=ExecutionResultConfirmed(retcode,deal);
   }
   else executionConfirmed=true; // PAPER mode confirms only the simulated decision.

   if(executionConfirmed)
   {
      bool reservationConfirmed=ConfirmH1Entry(entryBar);
      if(!reservationConfirmed)
      {
         // The original PENDING remains and blocks the H1 after restart. Never
         // release it after a valid execution merely because persistence failed.
         g_lastDecision="ORDEN CONFIRMADA, pero no se pudo persistir H1 CONFIRMED; se mantiene PENDING fail-closed";
         g_diagBlockReason=g_lastDecision;
      }
      else
      {
         g_lastDecision=EnableLiveTrading?"ORDEN ENVIADA":StringFormat("SEÑAL SIMULADA | %s | %.2fR | riesgo %.2f %s | SL %.2f | TP %.2f | RR %.2f",tag,targetR,actualRisk,contract.accountCurrency,sl,tp,rr);
         g_diagBlockReason="";
      }
      g_lastScore=score; g_lastSetup=setup; g_lastDirection=direction>0?"LONG":"SHORT";
      g_entryUsedThisBar=true;
      g_armed=false;
      g_monitorState="EN OPERACION";
      g_monitorReason="Entrada aceptada; no se permiten nuevas entradas en esta H1.";
      AppendDealLog(g_lastDirection,score,setup,targetAmount,lots,sl,tp,reason);
      PrintFormat("[GoldScout] DECISION=%s | %s %s score=%d techL=%d techS=%d risk=%.2f %s lots=%.4f entry=%.2f SL=%.2f TP=%.2f RR=%.2f mode=%s",
         tag,g_lastDirection,setup,score,g_diagLongFinal,g_diagShortFinal,actualRisk,contract.accountCurrency,lots,price,sl,tp,rr,EnableLiveTrading?"LIVE":"PAPER");
      if(EnableLiveTrading)
      {
         double fillPrice=trade.ResultPrice(),filledRisk=0.0;
         bool fillRiskKnown=fillPrice>0.0 && RiskAtSL(type,fillPrice,sl,lots,filledRisk);
         bool withinDeviation=fillPrice>0.0 && MathAbs(fillPrice-price)<=MAX_EXECUTION_DEVIATION_POINTS*_Point+_Point*0.5;
         // This is an alert, not a promise that market gaps, broker-side slippage
         // or changing costs can never exceed the 5% planned-risk ceiling.
         if(!fillRiskKnown || !withinDeviation || filledRisk>plannedRisk+1e-6 || filledRisk>remainingDailyBudget+1e-6)
            PrintFormat("[GoldScout] ADVERTENCIA: fill live fuera del presupuesto planificado/deviación | fill=%.5f risk=%.2f %s planned=%.2f budget=%.2f",fillPrice,filledRisk,contract.accountCurrency,plannedRisk,remainingDailyBudget);
      }
   }
   else
   {
      bool released=false;
      if(ExecutionFailureIsSafelyFinal(requestSent,retcode,deal))
         released=ReleasePendingH1Entry(entryBar);
      if(released)
      {
         g_entryUsedThisBar=false;
         g_lastDecision=StringFormat("Orden rechazada sin fill; reserva H1 liberada | retcode=%d",retcode);
      }
      else
      {
         g_entryUsedThisBar=true;
         g_lastDecision=StringFormat("Resultado de orden ambiguo o persistencia fallida; reserva H1 PENDING retenida | retcode=%d",retcode);
      }
      g_diagBlockReason=g_lastDecision;
   }
   UpdateDashboard();
}

string ActiveTradeJson()
{
   if(!IsOurPosition()) return "null";
   long type=PositionGetInteger(POSITION_TYPE);
   ulong posId=(ulong)PositionGetInteger(POSITION_IDENTIFIER);
   string reason="La explicación detallada se guarda en xau_goldscout_trade_log.csv";
   return StringFormat("{\"ticket\":%I64u,\"symbol\":\"%s\",\"direction\":\"%s\",\"volume\":%.4f,\"open_price\":%.2f,\"sl\":%.2f,\"tp\":%.2f,\"profit\":%.2f,\"comment\":\"%s\",\"reason\":\"%s\"}",
      (ulong)PositionGetInteger(POSITION_TICKET),JsonEscape(_Symbol),type==POSITION_TYPE_BUY?"LONG":"SHORT",
      PositionGetDouble(POSITION_VOLUME),PositionGetDouble(POSITION_PRICE_OPEN),PositionGetDouble(POSITION_SL),
      PositionGetDouble(POSITION_TP),PositionGetDouble(POSITION_PROFIT),JsonEscape(PositionGetString(POSITION_COMMENT)),JsonEscape(reason));
}

string ExtractTag(string comment)
{
   int pos=StringFind(comment,"|R");
   if(pos>=0)
   {
      string t=StringSubstr(comment,pos+2);
      int sep=StringFind(t,"|");
      if(sep>=0) t=StringSubstr(t,0,sep);
      double r=StringToDouble(t);
      return (r>=GodTargetR-1e-9)?"TRADE GOD":"CHILL";
   }
   return "CHILL";
}

string ClosedTradesJson()
{
   datetime now=TimeTradeServer();
   datetime from=now-30*24*60*60;
   if(!HistorySelect(from,now)) return "[]";

   ulong exits[]; ulong posids[]; double profits[]; double vols[]; datetime times[];
   int found=0;
   uint deals=HistoryDealsTotal();
   for(int i=(int)deals-1; i>=0 && found<25; i--)
   {
      ulong ticket=HistoryDealGetTicket(i); if(ticket==0) continue;
      if(HistoryDealGetString(ticket,DEAL_SYMBOL)!=_Symbol) continue;
      if((long)HistoryDealGetInteger(ticket,DEAL_MAGIC)!=MagicNumber) continue;
      long entry=HistoryDealGetInteger(ticket,DEAL_ENTRY);
      if(entry!=DEAL_ENTRY_OUT && entry!=DEAL_ENTRY_OUT_BY) continue;
      ArrayResize(exits,found+1); ArrayResize(posids,found+1); ArrayResize(profits,found+1); ArrayResize(vols,found+1); ArrayResize(times,found+1);
      exits[found]=ticket; posids[found]=(ulong)HistoryDealGetInteger(ticket,DEAL_POSITION_ID);
      profits[found]=HistoryDealGetDouble(ticket,DEAL_PROFIT)+HistoryDealGetDouble(ticket,DEAL_SWAP)+HistoryDealGetDouble(ticket,DEAL_COMMISSION);
      vols[found]=HistoryDealGetDouble(ticket,DEAL_VOLUME); times[found]=(datetime)HistoryDealGetInteger(ticket,DEAL_TIME);
      found++;
   }

   string out="[";
   for(int i=0;i<found;i++)
   {
      string comment=""; string direction="-"; string reason="La explicación detallada se guarda en xau_goldscout_trade_log.csv";
      if(HistorySelectByPosition(posids[i]))
      {
         uint pd=HistoryDealsTotal();
         for(uint j=0;j<pd;j++)
         {
            ulong dt=HistoryDealGetTicket(j); if(dt==0) continue;
            if(HistoryDealGetInteger(dt,DEAL_ENTRY)==DEAL_ENTRY_IN)
            {
               comment=HistoryDealGetString(dt,DEAL_COMMENT);
               long typ=HistoryDealGetInteger(dt,DEAL_TYPE);
               direction=(typ==DEAL_TYPE_BUY?"LONG":"SHORT");
               break;
            }
         }
      }
      string tag=ExtractTag(comment);
      if(i>0) out+=",";
      out+=StringFormat("{\"status\":\"CERRADO\",\"ticket\":%I64u,\"position_id\":%I64u,\"time\":\"%s\",\"direction\":\"%s\",\"volume\":%.4f,\"profit\":%.2f,\"tag\":\"%s\",\"comment\":\"%s\",\"reason\":\"%s\"}",
         exits[i],posids[i],TimeToString(times[i],TIME_DATE|TIME_SECONDS),direction,vols[i],profits[i],tag,JsonEscape(comment),JsonEscape(reason));
   }
   out+="]";
   return out;
}

void UpdateDashboard()
{
   if(!WriteDashboard) return;
   double balance=AccountInfoDouble(ACCOUNT_BALANCE), equity=AccountInfoDouble(ACCOUNT_EQUITY);
   string accountCurrency=AccountInfoString(ACCOUNT_CURRENCY);
   if(accountCurrency=="") accountCurrency="UNKNOWN";
   string active=ActiveTradeJson();

   int h=FileOpen(DashboardFile,FILE_COMMON|FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h==INVALID_HANDLE) return;

   string json="{";
   json += StringFormat("\"updated_at\":\"%s\",",JsonEscape(TimeToString(TimeTradeServer(),TIME_DATE|TIME_SECONDS)));
   json += StringFormat("\"symbol\":\"%s\",\"timeframe\":\"H1\",",JsonEscape(_Symbol));
   json += StringFormat("\"live_trading\":%s,",EnableLiveTrading?"true":"false");
   json += StringFormat("\"account_currency\":\"%s\",\"balance\":%.2f,\"equity\":%.2f,\"daily_pnl\":%.2f,\"risk_amount\":%.2f,\"risk_percent\":%.2f,",JsonEscape(accountCurrency),balance,equity,TodayClosedProfit(),PlannedRiskAmount(),RiskPercent);
   json += StringFormat("\"stop_mode\":\"ATR+STRUCTURE\",\"chill_r\":%.2f,\"god_r\":%.2f,\"min_rr\":%.2f,\"last_score\":%d,\"last_setup\":\"%s\",\"last_direction\":\"%s\",",ChillTargetR,GodTargetR,MinRewardRisk,g_lastScore,JsonEscape(g_lastSetup),JsonEscape(g_lastDirection));
   json += StringFormat("\"last_decision\":\"%s\",",JsonEscape(g_lastDecision));
   json += StringFormat("\"analysis\":{\"bar\":\"%s\",\"h4_trend\":\"%s\",\"h1_trend\":\"%s\",\"ema_fast\":%.2f,\"ema_slow\":%.2f,\"h4_ema_fast\":%.2f,\"h4_ema_slow\":%.2f,\"rsi\":%.2f,\"adx\":%.2f,\"atr\":%.2f,\"volume_current\":%.0f,\"volume_avg\":%.0f,\"volume_state\":\"%s\",\"structure\":\"%s\",\"long_tech\":%d,\"short_tech\":%d,\"long_news_points\":%d,\"short_news_points\":%d,\"long_final\":%d,\"short_final\":%d,\"candidate\":\"%s\",\"block_reason\":\"%s\",\"technical_reason\":\"%s\",\"news_reason\":\"%s\"},",
      TimeToString(g_diagBar,TIME_DATE|TIME_MINUTES),JsonEscape(g_diagH4Trend),JsonEscape(g_diagH1Trend),g_diagEmaFast,g_diagEmaSlow,g_diagH4Fast,g_diagH4Slow,g_diagRSI,g_diagADX,g_diagATR,g_diagCurVol,g_diagAvgVol,JsonEscape(g_diagVolume),JsonEscape(g_diagStructure),g_diagLongTech,g_diagShortTech,g_diagLongNewsPts,g_diagShortNewsPts,g_diagLongFinal,g_diagShortFinal,JsonEscape(g_lastDirection),JsonEscape(g_diagBlockReason),JsonEscape(g_diagTechnicalReason),JsonEscape(g_diagNewsReason));
   json += StringFormat("\"monitor\":{\"state\":\"%s\",\"candidate\":\"%s\",\"score\":%d,\"boost_l\":%d,\"boost_s\":%d,\"age_sec\":%I64d,\"entry_used_this_bar\":%s,\"reason\":\"%s\"},",
      JsonEscape(g_monitorState),JsonEscape(g_armedDirection>0?"LONG":(g_armedDirection<0?"SHORT":"-")),g_lastScore,g_intrabarLongBoost,g_intrabarShortBoost,g_monitorAgeSec,g_entryUsedThisBar?"true":"false",JsonEscape(g_monitorReason));
   json += StringFormat("\"news\":{\"available\":%s,\"updated_at\":\"%s\",\"bias\":%d,\"confidence\":%d,\"risk\":\"%s\",\"direction\":\"%s\",\"summary\":\"%s\",\"article_count\":%d},",
      (g_newsAvailable && g_newsUpdated!="")?"true":"false",JsonEscape(g_newsUpdated),g_newsBias,g_newsConfidence,JsonEscape(g_newsRisk),JsonEscape(g_newsDirection),JsonEscape(g_newsSummary),g_newsCount);
   json += StringFormat("\"active_trade\":%s,",active);
   json += StringFormat("\"closed_trades\":%s",ClosedTradesJson());
   json += "}";
   FileWriteString(h,json);
   FileClose(h);
}

void InitIndicators()
{
   hEmaFast=iMA(_Symbol,PERIOD_H1,FastEMA,0,MODE_EMA,PRICE_CLOSE);
   hEmaSlow=iMA(_Symbol,PERIOD_H1,SlowEMA,0,MODE_EMA,PRICE_CLOSE);
   hEmaHTFFast=iMA(_Symbol,PERIOD_H4,HTFFastEMA,0,MODE_EMA,PRICE_CLOSE);
   hEmaHTFSlow=iMA(_Symbol,PERIOD_H4,HTFSlowEMA,0,MODE_EMA,PRICE_CLOSE);
   hRSI=iRSI(_Symbol,PERIOD_H1,RSIPeriod,PRICE_CLOSE);
   hADX=iADX(_Symbol,PERIOD_H1,ADXPeriod);
   hATR=iATR(_Symbol,PERIOD_H1,ATRPeriod);
}

bool IsGoldSymbol()
{
   // Allow broker suffixes such as XAUUSD.a, XAUUSDm, etc.
   return (StringFind(_Symbol, "XAUUSD") == 0);
}

int OnInit()
{
   GS_ClearPatternDiagnostic(g_patternDiagnostic);
   GS_ClearContinuationPatternDiagnostic(g_continuationPatternDiagnostic);
   if(!IsGoldSymbol())
   {
      Print("[GoldScout] BLOQUEADO: este EA solo funciona en XAUUSD. Simbolo actual: ", _Symbol);
      return(INIT_FAILED);
   }
   string configMsg="";
   if(!SafetyInputsValid(configMsg))
   {
      Print("[GoldScout] BLOQUEADO: ",configMsg);
      return(INIT_PARAMETERS_INCORRECT);
   }
   BrokerContractSpec startupContract;
   string startupContractMsg="";
   if(LoadBrokerContract(startupContract,startupContractMsg))
   {
      PrintFormat("[GoldScout] CONTRATO %s | account=%s profit=%s | tickSize=%.8f tickValue=%.8f profitTick=%.8f lossTick=%.8f | contract=%.4f | volume=%.8f..%.8f step=%.8f | stops=%d freeze=%d",
         _Symbol,startupContract.accountCurrency,startupContract.profitCurrency,startupContract.tickSize,startupContract.tickValue,
         startupContract.tickValueProfit,startupContract.tickValueLoss,startupContract.contractSize,startupContract.volumeMin,
         startupContract.volumeMax,startupContract.volumeStep,startupContract.stopsLevel,startupContract.freezeLevel);
   }
   else Print("[GoldScout] AVISO: contrato aún no disponible; entradas bloqueadas hasta poder validarlo: ",startupContractMsg);
   if(!InitializeEntryReservationState())
   {
      Print("[GoldScout] BLOQUEADO: no se pudo inicializar la reserva H1 persistente");
      return(INIT_FAILED);
   }
   g_peakEquity=AccountInfoDouble(ACCOUNT_EQUITY);
   g_lastH1Bar=iTime(_Symbol,PERIOD_H1,0);
   g_diagBar=g_lastH1Bar;
   ResetIntrabarPlan(g_lastH1Bar);
   trade.SetExpertMagicNumber(MagicNumber);
   InitIndicators();
   EventSetTimer(MathMax(1,TimerSeconds));
   LoadWorldNews();
   g_lastDecision=EnableLiveTrading?"LIVE habilitado — iniciando análisis inmediato":"PAPER MODE — iniciando análisis inmediato";
   string accountCurrency=AccountInfoString(ACCOUNT_CURRENCY);
   PrintFormat("[GoldScout] Iniciado en %s H1 | risk=%.2f%% | riskAmount=%.2f %s | stop=ATR+estructura | minRR=%.2f | mode=%s | intrabar=%s | timer=%ds | arm=%d | startup=IMMEDIATE",_Symbol,RiskPercent,PlannedRiskAmount(),accountCurrency,MinRewardRisk,EnableLiveTrading?"LIVE":"PAPER",UseIntrabarMonitoring?"ON":"OFF",TimerSeconds,ArmScoreThreshold);

   // Start the current H1 cycle immediately. Do not wait for the next H1 candle.
   // If indicator history is still loading, OnTimer() retries the same bar until it succeeds.
   TryTrade();
   if(g_diagATR>0.0 || g_diagRSI>0.0 || g_diagADX>0.0)
      g_startupAnalysisPending=false;
   UpdateDashboard();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(hEmaFast!=INVALID_HANDLE) IndicatorRelease(hEmaFast);
   if(hEmaSlow!=INVALID_HANDLE) IndicatorRelease(hEmaSlow);
   if(hEmaHTFFast!=INVALID_HANDLE) IndicatorRelease(hEmaHTFFast);
   if(hEmaHTFSlow!=INVALID_HANDLE) IndicatorRelease(hEmaHTFSlow);
   if(hRSI!=INVALID_HANDLE) IndicatorRelease(hRSI);
   if(hADX!=INVALID_HANDLE) IndicatorRelease(hADX);
   if(hATR!=INVALID_HANDLE) IndicatorRelease(hATR);
}

void OnTimer()
{
   if(!IsGoldSymbol()) return;
   LoadWorldNews();
   datetime bar=iTime(_Symbol,PERIOD_H1,0);
   if(bar<=0) return;

   // Startup retry: history/indicator buffers can need a few seconds to become ready.
   // Keep retrying the CURRENT H1 bar instead of waiting for the next candle.
   if(g_startupAnalysisPending && bar==g_lastH1Bar)
   {
      g_startupAttempts++;
      TryTrade();
      if(g_diagATR>0.0 || g_diagRSI>0.0 || g_diagADX>0.0)
      {
         g_startupAnalysisPending=false;
         PrintFormat("[GoldScout] Startup analysis ready on current H1 after %d attempt(s).",g_startupAttempts);
      }
      return;
   }

   if(bar!=g_lastH1Bar)
   {
      g_lastH1Bar=bar;
      g_diagBar=bar;
      g_startupAnalysisPending=false;
      g_startupAttempts=0;
      ResetIntrabarPlan(bar);
      PrintFormat("[GoldScout] Nueva vela H1 %s | news bias=%d conf=%d risk=%s dir=%s age-source=%s | monitoreo intrabar=%s",TimeToString(bar,TIME_DATE|TIME_MINUTES),g_newsBias,g_newsConfidence,g_newsRisk,g_newsDirection,g_newsUpdated,UseIntrabarMonitoring?"ON":"OFF");
      TryTrade();
      if(UseIntrabarMonitoring) MonitorIntrabar();
      return;
   }

   if(UseIntrabarMonitoring)
      MonitorIntrabar();
   else
      UpdateDashboard();
}

void OnTick()
{
   // Monitoring is timer-driven to keep the cadence controlled and auditable.
}
