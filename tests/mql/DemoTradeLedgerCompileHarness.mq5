#property strict
#include <GoldScout/DemoTradeLedger.mqh>

void OnStart()
{
   string value="";
   int at=0;
   if(!GSDL_ParseString("\"escaped \\\"quote\\\" and \\\\ slash\"",at,value,false) ||
      value!="escaped \"quote\" and \\ slash")
      Print("GSDL parser self-check failed");
   string legacy="{\"event_id\":\"E\",\"event\":\"SIGNAL\",\"source\":\"MT5\"}";
   if(!GSDL_ValidJsonObject(legacy))
      Print("GSDL legacy self-check failed");
   string valid="{\"event_id\":\"GSE2-test\",\"event\":\"ORDER_REJECTED\","
      "\"source\":\"MT5\",\"schema_version\":2,\"trade_id\":\"T\","
      "\"account_login\":123,\"deal_ticket\":\"18446744073709551615\","
      "\"commission\":-1.25,\"reason\":\"quote \\\"x\\\" \\\\ unicode \\u00F1\","
      "\"fees\":null}";
   if(!GSDL_ValidJsonObject(valid)) Print("GSDL valid v2 self-check failed");
   if(GSDL_ValidJsonObject("{\"event_id\":\"E\",\"event_id\":\"F\",\"event\":\"SIGNAL\",\"source\":\"MT5\"}"))
      Print("GSDL duplicate-key rejection failed");
   if(GSDL_ValidJsonObject("{\"event_id\":\"E\",\"event\":\"SIGNAL\",\"source\":\"MT5\",\"value\":1e+}"))
      Print("GSDL invalid-number rejection failed");

   // Runtime self-test when launched as a script in MT5: two ledger instances
   // loaded before a >256 KB append must still share unique durable attempts
   // and must never append an old critical event twice.
   long account=AccountInfoInteger(ACCOUNT_LOGIN);
   if(account<=0) return;
   string filename=StringFormat("GoldScout_LedgerHarness_%I64d_%I64u.jsonl",
      account,(ulong)GetTickCount());
   GoldScoutDemoLedger first,second;
   if(!first.Initialize(filename) || !second.Initialize(filename))
   { Print("GSDL harness initialize failed"); return; }
   string padding="";
   for(int i=0;i<1024;i++) padding+="x";
   string oldJson="",oldId="GSE2-HARNESS-0";
   for(int i=0;i<320;i++)
   {
      string id=StringFormat("GSE2-HARNESS-%d",i);
      string row=StringFormat("{\"event_id\":\"%s\",\"event\":\"SIGNAL\","
         "\"trade_id\":\"T-%d\",\"account_login\":%I64d,"
         "\"schema_version\":2,\"source\":\"MT5\",\"reason\":\"%s\"}",
         id,i,account,padding);
      if(i==0) oldJson=row;
      if(!first.Append(id,row)) PrintFormat("GSDL harness append failed %d",i);
   }
   if(!second.Append(oldId,oldJson)) Print("GSDL harness second-instance duplicate check failed");
   string attempt1="",attempt2="";
   if(!first.ClaimAttempt("GS-HARNESS",_Symbol,8202609,"LONG","MOMENTUM",
      "GOD",89,attempt1) ||
      !second.ClaimAttempt("GS-HARNESS",_Symbol,8202609,"LONG","MOMENTUM",
      "GOD",89,attempt2) || attempt1==attempt2)
      Print("GSDL harness attempt uniqueness failed");
   if(!first.AttemptMatches(attempt1,"GS-HARNESS",_Symbol,8202609) ||
      first.AttemptMatches(attempt1,"GS-OTHER",_Symbol,8202609) ||
      first.AttemptMatches(attempt1,"GS-HARNESS",_Symbol,8202610))
      Print("GSDL harness claim scope failed");
   int handle=FileOpen(filename,FILE_COMMON|FILE_READ|FILE_TXT|FILE_ANSI,0,CP_UTF8);
   int matches=0;
   if(handle!=INVALID_HANDLE)
   {
      while(!FileIsEnding(handle))
      {
         string row=FileReadString(handle);
         if(GSDL_JsonField(row,"event_id")==oldId) matches++;
      }
      FileClose(handle);
   }
   if(matches!=1) PrintFormat("GSDL harness duplicate count=%d",matches);
   FileDelete(filename,FILE_COMMON);
}
