#property strict
#property version   "1.00"
#property description "GoldScout H1 - XAUUSD autonomous EA with fixed USD risk, trend/pullback scoring, news filter and dashboard JSON."

#include <Trade/Trade.mqh>

CTrade trade;

input group "=== Execution ==="
input bool   EnableLiveTrading      = false; // FALSE = analysis only / paper mode
input long   MagicNumber             = 8202609;
input int    TimerSeconds            = 5;
input int    MinScoreToTrade         = 74;
input bool   OnePositionAtATime      = true;
input bool   OneDecisionPerHour      = true; // compatibilidad; ya no bloquea el monitoreo intrabar
input bool   UseIntrabarMonitoring   = true;
input int    ArmScoreThreshold       = 58; // score minimo para armar un setup durante la vela


input group "=== Risk / Targets ==="
input double RiskPercent             = 5.0;  // risk as % of current equity per trade
input double ChillTargetR            = 1.25; // weaker structure: target >= 1.25R
input double GodTargetR              = 2.0;  // clear trend: target >= 2R
input double DailyLossLimitUSD       = 20.0; // hard daily stop in USD
input double MaxDrawdownPercent      = 15.0; // equity drawdown from peak
input double MaxSpreadUSD            = 0.60; // max bid/ask spread on gold
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

double NormalizeVolumeDown(double lots)
{
   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0) step = minLot;
   if(step <= 0.0) return 0.0;
   lots = MathMin(lots, maxLot);
   double normalized = MathFloor(lots / step + 1e-9) * step;
   if(normalized < minLot) return 0.0;
   return NormalizeDouble(normalized, 8);
}

bool IsOurPosition()
{
   if(!PositionSelect(_Symbol)) return false;
   long magic = (long)PositionGetInteger(POSITION_MAGIC);
   return magic == MagicNumber;
}

double TodayClosedProfit()
{
   datetime now = TimeTradeServer();
   MqlDateTime dt; TimeToStruct(now, dt);
   dt.hour = 0; dt.min = 0; dt.sec = 0;
   datetime start = StructToTime(dt);
   if(!HistorySelect(start, now)) return 0.0;
   double total = 0.0;
   uint deals = HistoryDealsTotal();
   for(uint i=0; i<deals; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol) continue;
      if((long)HistoryDealGetInteger(ticket, DEAL_MAGIC) != MagicNumber) continue;
      long entry = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
         total += HistoryDealGetDouble(ticket, DEAL_PROFIT) + HistoryDealGetDouble(ticket, DEAL_SWAP) + HistoryDealGetDouble(ticket, DEAL_COMMISSION);
   }
   return total;
}

bool RiskGuardsPass()
{
   if(!EnableLiveTrading) return true;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(g_peakEquity <= 0.0) g_peakEquity = equity;
   if(equity > g_peakEquity) g_peakEquity = equity;
   if(g_peakEquity > 0.0)
   {
      double dd = 100.0 * (g_peakEquity - equity) / g_peakEquity;
      if(dd >= MaxDrawdownPercent)
      {
         g_lastDecision = "Bloqueado: drawdown máximo alcanzado";
         return false;
      }
   }

   double daily = TodayClosedProfit();
   if(daily <= -MathAbs(DailyLossLimitUSD))
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

double PlannedRiskUSD()
{
   double equity=AccountInfoDouble(ACCOUNT_EQUITY);
   return MathMax(0.0,equity*MathAbs(RiskPercent)/100.0);
}

bool PositionSizeForRisk(ENUM_ORDER_TYPE type,double priceOpen,double slPrice,double riskUSD,double &lots)
{
   double lossOneLot=0.0;
   if(!OrderCalcProfit(type,_Symbol,1.0,priceOpen,slPrice,lossOneLot)) return false;
   lossOneLot=MathAbs(lossOneLot);
   if(lossOneLot<=0.0 || riskUSD<=0.0) return false;
   lots=NormalizeVolumeDown(riskUSD/lossOneLot);
   return lots>0.0;
}

bool RiskAtSL(ENUM_ORDER_TYPE type,double priceOpen,double slPrice,double lots,double &riskUSD)
{
   double p=0.0;
   if(!OrderCalcProfit(type,_Symbol,lots,priceOpen,slPrice,p)) return false;
   riskUSD=MathAbs(p);
   return riskUSD>0.0;
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

bool TPPriceForMoney(ENUM_ORDER_TYPE type,double priceOpen,double lots,double targetUSD,double &tpPrice)
{
   double tickSize=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   if(tickSize<=0.0 || targetUSD<=0.0) return false;
   double lo,hi,mid,profit;
   if(type==ORDER_TYPE_BUY)
   {
      lo=priceOpen; hi=priceOpen+500.0;
      for(int k=0;k<50;k++)
      {
         mid=(lo+hi)/2.0; profit=0.0;
         if(!OrderCalcProfit(type,_Symbol,lots,priceOpen,mid,profit)) return false;
         if(profit>=targetUSD) hi=mid; else lo=mid;
      }
      tpPrice=hi;
   }
   else
   {
      lo=priceOpen-500.0; hi=priceOpen;
      for(int k=0;k<50;k++)
      {
         mid=(lo+hi)/2.0; profit=0.0;
         if(!OrderCalcProfit(type,_Symbol,lots,priceOpen,mid,profit)) return false;
         if(profit>=targetUSD) lo=mid; else hi=mid;
      }
      tpPrice=lo;
   }
   tpPrice=NormalizeDouble(tpPrice,(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS));
   return true;
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
   int priorHiIdx=iHighest(_Symbol,PERIOD_H1,MODE_HIGH,StructureLookback,StructureLookback+2);
   int priorLoIdx=iLowest(_Symbol,PERIOD_H1,MODE_LOW,StructureLookback,StructureLookback+2);
   double priorHigh=priorHiIdx>=0?iHigh(_Symbol,PERIOD_H1,priorHiIdx):recentHigh;
   double priorLow=priorLoIdx>=0?iLow(_Symbol,PERIOD_H1,priorLoIdx):recentLow;
   double avgVol=RecentVolumeAverage(2);
   double curVol=(double)iVolume(_Symbol,PERIOD_H1,1);

   bool bullHTF = h4F > h4S;
   bool bearHTF = h4F < h4S;
   bool bullLTF = emaF > emaS;
   bool bearLTF = emaF < emaS;
   bool strongTrend = adx >= MinADX;
   bool adxBuild = adx >= 16.0;
   bool volOK = (avgVol>0.0 && curVol >= avgVol*1.05);
   bool hh = recentHigh > priorHigh + atr*0.20;
   bool hl = recentLow  > priorLow  + atr*0.20;
   bool lh = recentHigh < priorHigh - atr*0.20;
   bool ll = recentLow  < priorLow  - atr*0.20;

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

   bool structureLong = breakLong || hh || hl || pullLong;
   bool structureShort = breakShort || ll || lh || pullShort;
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
   // Oversold/overbought only helps when it agrees with an H4 pullback thesis.
   if(htfPullLong) longScore+=5;
   if(htfPullShort) shortScore+=5;

   if(structureLong) longScore+=15;
   if(structureShort) shortScore+=15;
   if(pullLong) longScore+=10;
   if(pullShort) shortScore+=10;
   if(breakLong) longScore+=10;
   if(breakShort) shortScore+=10;
   if(momLong) longScore+=5;
   if(momShort) shortScore+=5;
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
   else if(hh || hl || lh || ll) g_diagStructure=(hh||hl)?"ESTRUCTURA ALCISTA":"ESTRUCTURA BAJISTA";
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

   g_diagTechnicalReason=StringFormat("Tecnico L=%d/S=%d | H4=%s | H1=%s | RSI=%.1f | ADX=%.1f | ATR=%.2f | volumen=%s | estructura=%s | HH=%s HL=%s LH=%s LL=%s",
      longScore,shortScore,g_diagH4Trend,g_diagH1Trend,rsi,adx,atr,g_diagVolume,g_diagStructure,
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
   g_entryUsedThisBar=false;
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

void MonitorIntrabar()
{
   if(!UseIntrabarMonitoring || g_entryUsedThisBar)
   {
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
   UpdateDashboard();
}

bool CanOpenTrade()
{
   if(OnePositionAtATime && IsOurPosition())
   {
      g_lastDecision="Esperando: ya existe una posición activa";
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

void AppendDealLog(string direction, int score, string setup, double targetUSD, double lots, double sl, double tp, string reason)
{
   int h=FileOpen("xau_goldscout_trade_log.csv", FILE_COMMON|FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI, ';');
   if(h==INVALID_HANDLE) return;
   FileSeek(h,0,SEEK_END);
   if(FileTell(h)==0) FileWrite(h,"time","symbol","direction","score","setup","targetR","targetUSD","lots","sl","tp","reason");
   FileWrite(h,TimeToString(TimeTradeServer(),TIME_DATE|TIME_SECONDS),_Symbol,direction,score,setup,"",DoubleToString(targetUSD,2),DoubleToString(lots,2),DoubleToString(sl,_Digits),DoubleToString(tp,_Digits),reason);
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
   double atr=0.0;
   if(!GetValue(hATR,0,1,atr) || !BuildDynamicStop(direction,price,atr,g_tempSL))
   {
      g_lastDecision="Bloqueado: no se pudo construir SL dinamico";
      g_diagBlockReason=g_lastDecision;
      UpdateDashboard(); return;
   }
   double sl=g_tempSL;
   int stopsLevel=(int)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL);
   double minDist=stopsLevel*_Point;
   if(direction>0) sl=MathMin(sl,price-minDist); else sl=MathMax(sl,price+minDist);
   sl=NormalizeDouble(sl,_Digits);

   double plannedRisk=PlannedRiskUSD(),lots=0.0;
   if(!PositionSizeForRisk(type,price,sl,plannedRisk,lots))
   {
      g_lastDecision=StringFormat("Bloqueado: lote minimo del broker impide arriesgar %.2f USD",plannedRisk);
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   double actualRisk=0.0;
   if(!RiskAtSL(type,price,sl,lots,actualRisk))
   {
      g_lastDecision="Bloqueado: no se pudo calcular riesgo al SL";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   double targetUSD=actualRisk*targetR;
   double tp=0.0;
   if(!TPPriceForMoney(type,price,lots,targetUSD,tp))
   {
      g_lastDecision="Bloqueado: no se pudo calcular TP";
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }

   double slDist=MathAbs(price-sl),tpDist=MathAbs(tp-price);
   double rr=slDist>0.0?tpDist/slDist:0.0;
   if(rr+1e-9<MinRewardRisk)
   {
      g_lastDecision=StringFormat("Bloqueado: R:R %.2f < minimo %.2f",rr,MinRewardRisk);
      g_diagBlockReason=g_lastDecision; UpdateDashboard(); return;
   }
   tp=NormalizeDouble(tp,_Digits);

   string tag=(targetR>=GodTargetR-1e-9?"TRADE GOD":"CHILL");
   string comment=StringFormat("GOLDscout|%s|S%d|%s|R%.2f|risk%.2f|RR%.2f",direction>0?"BUY":"SELL",score,setup,targetR,actualRisk,rr);
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(30);

   bool sent=false;
   if(EnableLiveTrading) sent=trade.PositionOpen(_Symbol,type,lots,price,sl,tp,comment);
   else sent=true;

   if(sent)
   {
      g_lastDecision=EnableLiveTrading?"ORDEN ENVIADA":StringFormat("SEÑAL SIMULADA | %s | %.2fR | riesgo $%.2f | SL %.2f | TP %.2f | RR %.2f",tag,targetR,actualRisk,sl,tp,rr);
      g_lastScore=score; g_lastSetup=setup; g_lastDirection=direction>0?"LONG":"SHORT";
      g_diagBlockReason="";
      g_entryUsedThisBar=true;
      g_armed=false;
      g_monitorState="EN OPERACION";
      g_monitorReason="Entrada aceptada; no se permiten nuevas entradas en esta H1.";
      AppendDealLog(g_lastDirection,score,setup,targetUSD,lots,sl,tp,reason);
      PrintFormat("[GoldScout] DECISION=%s | %s %s score=%d techL=%d techS=%d risk=%.2f lots=%.4f entry=%.2f SL=%.2f TP=%.2f RR=%.2f mode=%s",
         tag,g_lastDirection,setup,score,g_diagLongFinal,g_diagShortFinal,actualRisk,lots,price,sl,tp,rr,EnableLiveTrading?"LIVE":"PAPER");
   }
   else
   {
      g_lastDecision=StringFormat("Error al enviar orden: retcode=%d",trade.ResultRetcode());
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
   string active=ActiveTradeJson();

   int h=FileOpen(DashboardFile,FILE_COMMON|FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h==INVALID_HANDLE) return;

   string json="{";
   json += StringFormat("\"updated_at\":\"%s\",",JsonEscape(TimeToString(TimeTradeServer(),TIME_DATE|TIME_SECONDS)));
   json += StringFormat("\"symbol\":\"%s\",\"timeframe\":\"H1\",",JsonEscape(_Symbol));
   json += StringFormat("\"live_trading\":%s,",EnableLiveTrading?"true":"false");
   json += StringFormat("\"balance\":%.2f,\"equity\":%.2f,\"daily_pnl\":%.2f,\"risk_usd\":%.2f,\"risk_percent\":%.2f,",balance,equity,TodayClosedProfit(),PlannedRiskUSD(),RiskPercent);
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
   if(!IsGoldSymbol())
   {
      Print("[GoldScout] BLOQUEADO: este EA solo funciona en XAUUSD. Simbolo actual: ", _Symbol);
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
   PrintFormat("[GoldScout] Iniciado en %s H1 | risk=%.2f%% | riskUSD=%.2f | stop=ATR+estructura | minRR=%.2f | mode=%s | intrabar=%s | timer=%ds | arm=%d | startup=IMMEDIATE",_Symbol,RiskPercent,PlannedRiskUSD(),MinRewardRisk,EnableLiveTrading?"LIVE":"PAPER",UseIntrabarMonitoring?"ON":"OFF",TimerSeconds,ArmScoreThreshold);

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
