#ifndef GOLDSCOUT_MARKET_OBSERVER_MQH
#define GOLDSCOUT_MARKET_OBSERVER_MQH

#include <GoldScout/MarketStructure.mqh>
#include <GoldScout/MarketOutcomes.mqh>

// Observation-only market dataset. Nothing in this module returns a trading
// signal or modifies an EA score.
struct GoldScoutObserverContext
{
   string session;
   int    newsBias;
   string newsDataRisk;
   int    longScore;
   int    shortScore;
   string decision;
   string decisionReason;
   string monitorState;
   string direction;
   bool   liveTrading;
   double currentTP;
   double currentRR;
   double v2TP;
   double v2RR;
   double selectedTP;
   double selectedRR;
   string tpMode;
   double tpStructureLevel;
   string tpStructureConfidence;
   string executionState;
   string signalEventId;
};

string GSMO_JsonEscape(string value)
{
   StringReplace(value,"\\","\\\\");
   StringReplace(value,"\"","\\\"");
   StringReplace(value,"\r"," ");
   StringReplace(value,"\n"," ");
   StringReplace(value,"\t"," ");
   return value;
}

string GSMO_Lower(string value)
{
   StringToLower(value);
   return value;
}

bool GSMO_Contains(const string value,const string fragment)
{
   return StringFind(GSMO_Lower(value),GSMO_Lower(fragment))>=0;
}

string GSMO_Number(const double value,const int digits=8)
{
   if(!MathIsValidNumber(value)) return "null";
   return DoubleToString(value,digits);
}

string GSMO_TimeframeName(const ENUM_TIMEFRAMES timeframe)
{
   if(timeframe==PERIOD_M15) return "M15";
   if(timeframe==PERIOD_H1) return "H1";
   if(timeframe==PERIOD_H4) return "H4";
   return "UNSUPPORTED";
}

string GSMO_DirectionName(const int direction)
{
   if(direction>0) return "LONG";
   if(direction<0) return "SHORT";
   return "NONE";
}

string GSMO_DecisionOutcome(const GoldScoutObserverContext &context)
{
   string text=GSMO_Lower(context.decision+" "+context.decisionReason);
   if(context.liveTrading &&
      (StringFind(text,"orden enviada")>=0 || StringFind(text,"orden confirmada")>=0 ||
       StringFind(text,"order_filled")>=0 || StringFind(text,"position_open")>=0))
      return "TRADE_TAKEN";
   if(StringFind(text,"señal simulada")>=0 || StringFind(text,"senal simulada")>=0)
      return "NO_TRADE";
   if(StringFind(text,"margen")>=0 || StringFind(text,"margin")>=0)
      return "BLOCKED_MARGIN";
   if(StringFind(text,"noticia")>=0 || StringFind(text,"calendario")>=0 ||
      StringFind(text,"news")>=0)
      return "BLOCKED_NEWS";
   if(StringFind(text,"riesgo")>=0 || StringFind(text,"drawdown")>=0 ||
      StringFind(text,"pérdida diaria")>=0 || StringFind(text,"perdida diaria")>=0 ||
      StringFind(text,"spread")>=0 || StringFind(text,"presupuesto")>=0)
      return "BLOCKED_RISK";
   if(StringFind(text,"score final")>=0 || StringFind(text,"sin setup")>=0 ||
      StringFind(text,"esperando condiciones")>=0 || StringFind(text,"armado ")>=0)
      return "INSUFFICIENT_SCORE";
   return "NO_TRADE";
}

uint GSMO_Hash(const string value)
{
   uint hash=2166136261;
   int length=StringLen(value);
   for(int i=0;i<length;i++)
   {
      hash^=(uint)StringGetCharacter(value,i);
      hash*=16777619;
   }
   return hash;
}

class GoldScoutMarketObserver
{
private:
   string          m_symbol;
   string          m_filename;
   ENUM_TIMEFRAMES m_timeframes[3];
   int             m_rsi[3];
   int             m_adx[3];
   int             m_atr[3];
   int             m_ema20[3];
   int             m_ema200[3];
   datetime        m_lastClosedBar[3];
   string          m_recentIds[];
   int             m_recentLimit;
   int             m_recentCursor;
   int             m_lookback;
   GoldScoutPivotConfig m_pivotConfig;
   bool            m_initialized;
   datetime        m_lastErrorLog;
   string          m_lastStateIdentity;
   GoldScoutMarketOutcomeLabeler m_outcomeLabeler;

   bool ReadValue(const int handle,const int buffer,const int shift,double &value)
   {
      double values[];
      ArraySetAsSeries(values,true);
      if(handle==INVALID_HANDLE || CopyBuffer(handle,buffer,shift,1,values)!=1)
         return false;
      value=values[0];
      return MathIsValidNumber(value);
   }

   bool IsRecentId(const string eventId)
   {
      for(int i=0;i<ArraySize(m_recentIds);i++)
         if(m_recentIds[i]==eventId) return true;
      return false;
   }

   void RememberId(const string eventId)
   {
      if(eventId=="" || IsRecentId(eventId)) return;
      int size=ArraySize(m_recentIds);
      if(size<m_recentLimit)
      {
         if(ArrayResize(m_recentIds,size+1)==size+1)
            m_recentIds[size]=eventId;
         return;
      }
      if(size<=0) return;
      m_recentIds[m_recentCursor]=eventId;
      m_recentCursor=(m_recentCursor+1)%size;
   }

   void LoadRecentIds()
   {
      int handle=FileOpen(m_filename,FILE_COMMON|FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE,0,CP_UTF8);
      if(handle==INVALID_HANDLE) return;
      long size=(long)FileSize(handle);
      long start=MathMax((long)0,size-(long)(256*1024));
      if(!FileSeek(handle,start,SEEK_SET))
      {
         FileClose(handle);
         return;
      }
      if(start>0 && !FileIsEnding(handle)) FileReadString(handle);
      string marker="\"event_id\":\"";
      while(!FileIsEnding(handle))
      {
         string line=FileReadString(handle);
         int begin=StringFind(line,marker);
         if(begin<0) continue;
         begin+=StringLen(marker);
         int finish=StringFind(line,"\"",begin);
         if(finish>begin) RememberId(StringSubstr(line,begin,finish-begin));
      }
      FileClose(handle);
   }

   void LogPersistenceError()
   {
      datetime now=TimeTradeServer();
      if(now<=0) now=TimeLocal();
      if(now<=0 || (m_lastErrorLog>0 && (long)(now-m_lastErrorLog)<60)) return;
      m_lastErrorLog=now;
      PrintFormat("[GoldScout][MARKET_OBSERVER] persistence error=%d | trading remains unaffected",GetLastError());
   }

   bool AppendRecord(const string eventId,const string json)
   {
      if(IsRecentId(eventId)) return true;
      ResetLastError();
      int handle=FileOpen(m_filename,FILE_COMMON|FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ|FILE_SHARE_WRITE,0,CP_UTF8);
      if(handle==INVALID_HANDLE)
      {
         LogPersistenceError();
         return false;
      }
      bool ok=FileSeek(handle,0,SEEK_END);
      if(ok) ok=FileWriteString(handle,json+"\n")>0;
      if(ok) FileFlush(handle);
      FileClose(handle);
      if(!ok)
      {
         LogPersistenceError();
         return false;
      }
      RememberId(eventId);
      return true;
   }

   bool MarketState(const int index,const int shift,
                    string &structure,string &breakout,string &momentum,
                    string &pullback,string &recovery,
                    double &rsi,double &adx,double &plusDi,double &minusDi,
                    double &atr,double &ema20,double &ema200)
   {
      if(!ReadValue(m_rsi[index],0,shift,rsi) ||
         !ReadValue(m_adx[index],0,shift,adx) ||
         !ReadValue(m_adx[index],1,shift,plusDi) ||
         !ReadValue(m_adx[index],2,shift,minusDi) ||
         !ReadValue(m_atr[index],0,shift,atr) ||
         !ReadValue(m_ema20[index],0,shift,ema20) ||
         !ReadValue(m_ema200[index],0,shift,ema200) || atr<=0.0)
         return false;

      GoldScoutPivot pivots[];
      GoldScoutStructureClassification classification;
      GS_ClearStructureClassification(classification);
      bool structureReady=GS_LoadConfirmedPivots(m_symbol,m_timeframes[index],m_atr[index],
         m_lookback,m_pivotConfig,pivots) &&
         GS_ClassifyConfirmedStructure(pivots,m_pivotConfig.toleranceAtr,
            SymbolInfoDouble(m_symbol,SYMBOL_POINT),classification);
      structure=structureReady ? GS_StructureStateName(classification.state) : "INSUFICIENTE";

      MqlRates rates[];
      ArraySetAsSeries(rates,true);
      if(CopyRates(m_symbol,m_timeframes[index],shift,2,rates)!=2) return false;
      double closeNow=rates[0].close;
      double closeBefore=rates[1].close;
      bool haveHigh=false,haveLow=false;
      double swingHigh=0.0,swingLow=0.0;
      if(structureReady) GS_LatestConfirmedSwingLevels(pivots,haveHigh,swingHigh,haveLow,swingLow);
      double buffer=MathMax(SymbolInfoDouble(m_symbol,SYMBOL_POINT),atr*0.05);

      int breakoutDirection=0;
      if(haveHigh && closeBefore<=swingHigh+buffer && closeNow>swingHigh+buffer)
         breakoutDirection=1;
      else if(haveLow && closeBefore>=swingLow-buffer && closeNow<swingLow-buffer)
         breakoutDirection=-1;
      breakout=GSMO_DirectionName(breakoutDirection);

      int momentumDirection=0;
      if(adx>=20.0 && plusDi>minusDi && rsi>=55.0)
         momentumDirection=1;
      else if(adx>=20.0 && minusDi>plusDi && rsi<=45.0)
         momentumDirection=-1;
      momentum=GSMO_DirectionName(momentumDirection);

      int pullbackDirection=0;
      if(classification.state==GOLDSCOUT_STRUCTURE_BULLISH && closeNow>=ema20 &&
         rates[0].low<=ema20+atr*0.20 && closeNow>ema200)
         pullbackDirection=1;
      else if(classification.state==GOLDSCOUT_STRUCTURE_BEARISH && closeNow<=ema20 &&
         rates[0].high>=ema20-atr*0.20 && closeNow<ema200)
         pullbackDirection=-1;
      pullback=GSMO_DirectionName(pullbackDirection);

      int recoveryDirection=0;
      if(classification.state==GOLDSCOUT_STRUCTURE_BULLISH &&
         closeBefore<=ema20 && closeNow>ema20)
         recoveryDirection=1;
      else if(classification.state==GOLDSCOUT_STRUCTURE_BEARISH &&
         closeBefore>=ema20 && closeNow<ema20)
         recoveryDirection=-1;
      recovery=GSMO_DirectionName(recoveryDirection);
      return true;
   }

   bool Capture(const int index,const int shift,const string snapshotType,
                const string identity,const GoldScoutObserverContext &context)
   {
      MqlRates rates[];
      ArraySetAsSeries(rates,true);
      if(CopyRates(m_symbol,m_timeframes[index],shift,1,rates)!=1) return false;

      string structure="INSUFICIENTE",breakout="NONE",momentum="NONE";
      string pullback="NONE",recovery="NONE";
      double rsi=0.0,adx=0.0,plusDi=0.0,minusDi=0.0,atr=0.0,ema20=0.0,ema200=0.0;
      if(!MarketState(index,shift,structure,breakout,momentum,pullback,recovery,
         rsi,adx,plusDi,minusDi,atr,ema20,ema200)) return false;

      datetime capturedAt=TimeTradeServer();
      if(capturedAt<=0) capturedAt=TimeCurrent();
      if(capturedAt<=0) return false;
      datetime outcomeAnchorAt=capturedAt;
      if(shift>0)
      {
         int timeframeSeconds=PeriodSeconds(m_timeframes[index]);
         if(timeframeSeconds>0)
            outcomeAnchorAt=(datetime)((long)rates[0].time+timeframeSeconds);
      }
      string timeframe=GSMO_TimeframeName(m_timeframes[index]);
      string eventId=StringFormat("MT5-%s-%s-%I64d-%s-%u",m_symbol,timeframe,
         (long)rates[0].time,snapshotType,GSMO_Hash(identity));
      if(IsRecentId(eventId))
      {
         m_outcomeLabeler.Track(eventId,outcomeAnchorAt,rates[0].close);
         return true;
      }

      string json="{";
      json+="\"event_id\":\""+GSMO_JsonEscape(eventId)+"\",";
      json+="\"source\":\"MT5\",\"observer_only\":true,\"score_effect\":0,";
      json+="\"snapshot_type\":\""+GSMO_JsonEscape(snapshotType)+"\",";
      json+="\"event\":\""+GSMO_JsonEscape(snapshotType)+"\",";
      json+=StringFormat("\"captured_at\":%I64d,\"timestamp\":%I64d,\"outcome_anchor_at\":%I64d,",
         (long)capturedAt,(long)rates[0].time,(long)outcomeAnchorAt);
      json+="\"symbol\":\""+GSMO_JsonEscape(m_symbol)+"\",\"timeframe\":\""+timeframe+"\",";
      json+="\"bar_closed\":"+(shift>0?"true":"false")+",";
      json+="\"open\":"+GSMO_Number(rates[0].open)+",\"high\":"+GSMO_Number(rates[0].high)+",";
      json+="\"low\":"+GSMO_Number(rates[0].low)+",\"close\":"+GSMO_Number(rates[0].close)+",";
      json+="\"rsi\":"+GSMO_Number(rsi,4)+",\"adx\":"+GSMO_Number(adx,4)+",";
      json+="\"plus_di\":"+GSMO_Number(plusDi,4)+",\"minus_di\":"+GSMO_Number(minusDi,4)+",";
      json+="\"atr\":"+GSMO_Number(atr)+",\"ema20\":"+GSMO_Number(ema20)+",\"ema200\":"+GSMO_Number(ema200)+",";
      json+="\"structure\":\""+GSMO_JsonEscape(structure)+"\",\"breakout\":\""+breakout+"\",";
      json+="\"momentum\":\""+momentum+"\",\"pullback\":\""+pullback+"\",\"recovery\":\""+recovery+"\",";
      json+="\"session\":\""+GSMO_JsonEscape(context.session)+"\",";
      json+=StringFormat("\"news_bias\":%d,",context.newsBias);
      json+="\"news_data_risk\":\""+GSMO_JsonEscape(context.newsDataRisk)+"\",";
      json+=StringFormat("\"long_score\":%d,\"short_score\":%d,",context.longScore,context.shortScore);
      json+="\"decision\":\""+GSMO_JsonEscape(GSMO_DecisionOutcome(context))+"\",";
      json+="\"decision_reason\":\""+GSMO_JsonEscape(context.decisionReason)+"\",";
      json+="\"goldscout_state\":\""+GSMO_JsonEscape(context.monitorState)+"\",";
      json+="\"current_tp\":"+GSMO_Number(context.currentTP)+",\"current_rr\":"+GSMO_Number(context.currentRR,4)+",";
      json+="\"v2_tp\":"+GSMO_Number(context.v2TP)+",\"v2_rr\":"+GSMO_Number(context.v2RR,4)+",";
      json+="\"selected_tp\":"+GSMO_Number(context.selectedTP)+",\"selected_rr\":"+GSMO_Number(context.selectedRR,4)+",";
      json+="\"tp_mode\":\""+GSMO_JsonEscape(context.tpMode)+"\",";
      json+="\"tp_structure_level\":"+GSMO_Number(context.tpStructureLevel)+",";
      json+="\"tp_structure_confidence\":\""+GSMO_JsonEscape(context.tpStructureConfidence)+"\",";
      json+="\"execution_state\":\""+GSMO_JsonEscape(context.executionState)+"\",";
      json+="\"signal_event_id\":\""+GSMO_JsonEscape(context.signalEventId)+"\",";
      json+="\"future_return_15m\":null,\"future_return_1h\":null,\"future_return_4h\":null,";
      json+="\"mfe_15m\":null,\"mae_15m\":null,\"mfe_1h\":null,\"mae_1h\":null,";
      json+="\"mfe_4h\":null,\"mae_4h\":null";
      json+="}";
      bool stored=AppendRecord(eventId,json);
      if(stored) m_outcomeLabeler.Track(eventId,outcomeAnchorAt,rates[0].close);
      return stored;
   }

public:
   GoldScoutMarketObserver()
   {
      m_symbol="";
      m_filename="market_observations.jsonl";
      m_timeframes[0]=PERIOD_M15;
      m_timeframes[1]=PERIOD_H1;
      m_timeframes[2]=PERIOD_H4;
      for(int i=0;i<3;i++)
      {
         m_rsi[i]=INVALID_HANDLE;
         m_adx[i]=INVALID_HANDLE;
         m_atr[i]=INVALID_HANDLE;
         m_ema20[i]=INVALID_HANDLE;
         m_ema200[i]=INVALID_HANDLE;
         m_lastClosedBar[i]=0;
      }
      m_recentLimit=512;
      m_recentCursor=0;
      m_lookback=120;
      m_initialized=false;
      m_lastErrorLog=0;
      m_lastStateIdentity="";
   }

   bool Initialize(const string symbol,const string filename,const string outcomeFilename,
                   const int rsiPeriod,const int adxPeriod,const int atrPeriod,
                   const int lookback,const GoldScoutPivotConfig &pivotConfig)
   {
      Shutdown();
      m_symbol=symbol;
      m_filename=filename;
      m_lookback=MathMax(20,lookback);
      m_pivotConfig=pivotConfig;
      if(m_symbol=="" || m_filename=="" || rsiPeriod<2 || adxPeriod<2 || atrPeriod<2 ||
         !GS_ValidatePivotConfig(m_pivotConfig)) return false;

      for(int i=0;i<3;i++)
      {
         m_rsi[i]=iRSI(m_symbol,m_timeframes[i],rsiPeriod,PRICE_CLOSE);
         m_adx[i]=iADX(m_symbol,m_timeframes[i],adxPeriod);
         m_atr[i]=iATR(m_symbol,m_timeframes[i],atrPeriod);
         m_ema20[i]=iMA(m_symbol,m_timeframes[i],20,0,MODE_EMA,PRICE_CLOSE);
         m_ema200[i]=iMA(m_symbol,m_timeframes[i],200,0,MODE_EMA,PRICE_CLOSE);
         if(m_rsi[i]==INVALID_HANDLE || m_adx[i]==INVALID_HANDLE ||
            m_atr[i]==INVALID_HANDLE || m_ema20[i]==INVALID_HANDLE ||
            m_ema200[i]==INVALID_HANDLE)
         {
            Shutdown();
            return false;
         }
      }
      m_initialized=true;
      LoadRecentIds();
      if(!m_outcomeLabeler.Initialize(m_symbol,m_filename,outcomeFilename))
         Print("[GoldScout][MARKET_OUTCOMES] no disponible; trading y observaciones continúan sin cambios");
      return true;
   }

   void Shutdown()
   {
      m_outcomeLabeler.Shutdown();
      for(int i=0;i<3;i++)
      {
         if(m_rsi[i]!=INVALID_HANDLE) IndicatorRelease(m_rsi[i]);
         if(m_adx[i]!=INVALID_HANDLE) IndicatorRelease(m_adx[i]);
         if(m_atr[i]!=INVALID_HANDLE) IndicatorRelease(m_atr[i]);
         if(m_ema20[i]!=INVALID_HANDLE) IndicatorRelease(m_ema20[i]);
         if(m_ema200[i]!=INVALID_HANDLE) IndicatorRelease(m_ema200[i]);
         m_rsi[i]=INVALID_HANDLE;
         m_adx[i]=INVALID_HANDLE;
         m_atr[i]=INVALID_HANDLE;
         m_ema20[i]=INVALID_HANDLE;
         m_ema200[i]=INVALID_HANDLE;
         m_lastClosedBar[i]=0;
      }
      ArrayResize(m_recentIds,0);
      m_recentCursor=0;
      m_initialized=false;
      m_lastStateIdentity="";
   }

   bool IsInitialized() const { return m_initialized; }

   void PollOutcomes(const int maximumEvents=8)
   {
      if(!m_initialized || !m_outcomeLabeler.IsInitialized()) return;
      m_outcomeLabeler.Poll(maximumEvents);
   }

   void PollClosedBars(const GoldScoutObserverContext &context)
   {
      if(!m_initialized) return;
      for(int i=0;i<3;i++)
      {
         datetime closedBar=iTime(m_symbol,m_timeframes[i],1);
         if(closedBar<=0 || closedBar==m_lastClosedBar[i]) continue;
         if(Capture(i,1,"BAR_CLOSE","BAR_CLOSE",context))
            m_lastClosedBar[i]=closedBar;
      }
   }

   void CaptureStateChange(const GoldScoutObserverContext &context)
   {
      if(!m_initialized) return;
      string decision=GSMO_Lower(context.decision);
      string snapshotType="";
      if(StringFind(decision,"armado")>=0)
         snapshotType="SIGNAL_ARMED";
      else if(context.liveTrading &&
         (StringFind(decision,"orden enviada")>=0 || StringFind(decision,"orden confirmada")>=0 ||
          StringFind(decision,"order_filled")>=0 || StringFind(decision,"position_open")>=0))
         snapshotType="TRADE_EXECUTED";
      else if(StringFind(decision,"bloqueado")>=0 || StringFind(decision,"orden rechazada")>=0 ||
              StringFind(decision,"resultado de orden ambiguo")>=0)
         snapshotType="RELEVANT_REJECTION";
      else if(context.monitorState!="")
         snapshotType="STATE_CHANGE";
      if(snapshotType=="") return;

      string outcome=GSMO_DecisionOutcome(context);
      string identity=snapshotType+"|"+outcome+"|"+context.monitorState+"|"+context.direction;
      if(identity==m_lastStateIdentity) return;
      if(Capture(1,0,snapshotType,identity,context))
         m_lastStateIdentity=identity;
   }
};

#endif
