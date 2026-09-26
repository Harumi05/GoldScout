#property copyright "GoldScout / Hali"
#property version   "1.00"
#property indicator_chart_window
#property indicator_plots 0

input int    RefreshMilliseconds = 1000;
input string OutputFile          = "hali_live_market.json";

string JsonEscapeLocal(string value)
{
   StringReplace(value,"\\","\\\\");
   StringReplace(value,"\"","\\\"");
   StringReplace(value,"\r","\\r");
   StringReplace(value,"\n","\\n");
   return value;
}

string BarJson(const ENUM_TIMEFRAMES timeframe,const string timeframeName)
{
   datetime barTime=iTime(_Symbol,timeframe,0);
   if(barTime<=0) return "null";

   double open=iOpen(_Symbol,timeframe,0);
   double high=iHigh(_Symbol,timeframe,0);
   double low=iLow(_Symbol,timeframe,0);
   double close=iClose(_Symbol,timeframe,0);
   long volume=(long)iVolume(_Symbol,timeframe,0);
   if(open<=0.0 || high<=0.0 || low<=0.0 || close<=0.0) return "null";

   return StringFormat(
      "{\"timeframe\":\"%s\",\"timestamp\":%I64d,\"open\":%.8f,\"high\":%.8f,\"low\":%.8f,\"close\":%.8f,\"volume\":%I64d,\"live\":true}",
      timeframeName,(long)barTime,open,high,low,close,volume
   );
}

void WriteLiveSnapshot()
{
   if(StringFind(_Symbol,"XAUUSD")!=0) return;

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol,tick)) return;

   double spread=(tick.bid>0.0 && tick.ask>0.0)?tick.ask-tick.bid:0.0;
   datetime serverNow=TimeTradeServer();
   long tickEpoch=(long)tick.time;
   long tickTimeMsc=(long)tick.time_msc;

   string json="{";
   json += StringFormat(
      "\"symbol\":\"%s\",\"server_time\":\"%s\",\"server_epoch\":%I64d,\"tick_epoch\":%I64d,\"tick_time_msc\":%I64d,\"bid\":%.8f,\"ask\":%.8f,\"last\":%.8f,\"spread\":%.8f,",
      JsonEscapeLocal(_Symbol),JsonEscapeLocal(TimeToString(serverNow,TIME_DATE|TIME_SECONDS)),
      (long)serverNow,tickEpoch,tickTimeMsc,tick.bid,tick.ask,tick.last,spread
   );
   json += "\"bars\":{";
   json += "\"M15\":"+BarJson(PERIOD_M15,"M15")+",";
   json += "\"H1\":"+BarJson(PERIOD_H1,"H1")+",";
   json += "\"H4\":"+BarJson(PERIOD_H4,"H4");
   json += "}}";

   int handle=FileOpen(OutputFile,FILE_COMMON|FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_SHARE_READ);
   if(handle==INVALID_HANDLE) return;
   FileWriteString(handle,json);
   FileFlush(handle);
   FileClose(handle);
}

int OnInit()
{
   if(StringFind(_Symbol,"XAUUSD")!=0)
   {
      Print("[HaliLiveFeed] Attach this indicator only to an XAUUSD chart.");
      return(INIT_FAILED);
   }

   int interval=MathMax(250,RefreshMilliseconds);
   if(!EventSetMillisecondTimer(interval))
   {
      PrintFormat("[HaliLiveFeed] Could not start timer. Error=%d",GetLastError());
      return(INIT_FAILED);
   }

   WriteLiveSnapshot();
   PrintFormat("[HaliLiveFeed] LIVE feed active every %d ms -> FILE_COMMON\\%s",interval,OutputFile);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   WriteLiveSnapshot();
}

int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   return(rates_total);
}
