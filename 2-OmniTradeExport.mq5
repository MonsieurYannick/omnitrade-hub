//+------------------------------------------------------------------+
//|  OmniTradeExport.mq5                                            |
//|  Exporte compte / historique / positions en JSON pour OmniTrade Hub |
//|  Compatible macOS, Windows et Linux (aucune DLL requise).        |
//|                                                                  |
//|  Installation :                                                  |
//|    1. MT5 -> Outils -> MetaQuotes Language Editor                |
//|    2. Fichier -> Nouveau -> Expert Advisor, coller ce code       |
//|    3. Compiler (F7), puis glisser l'EA sur un graphique          |
//|    4. Cocher « Autoriser le trading algorithmique »              |
//+------------------------------------------------------------------+
#property copyright "OmniTrade Hub"
#property version   "1.00"
#property strict

input int  RefreshSeconds = 10;    // Fréquence d'écriture (secondes)
input int  HistoryDays    = 365;   // Profondeur d'historique

string Esc(string s){ StringReplace(s,"\\","\\\\"); StringReplace(s,"\"","\\\""); return s; }
string TS(datetime t){ return TimeToString(t, TIME_DATE|TIME_SECONDS); }

void WriteFile(string name, string content){
   int h = FileOpen(name, FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h == INVALID_HANDLE){ Print("OmniTrade Hub: écriture impossible ", name); return; }
   FileWriteString(h, content);
   FileClose(h);
}

void ExportAccount(){
   string j = "{";
   j += "\"login\":"        + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   j += "\"name\":\""       + Esc(AccountInfoString(ACCOUNT_NAME)) + "\",";
   j += "\"server\":\""     + Esc(AccountInfoString(ACCOUNT_SERVER)) + "\",";
   j += "\"company\":\""    + Esc(AccountInfoString(ACCOUNT_COMPANY)) + "\",";
   j += "\"currency\":\""   + Esc(AccountInfoString(ACCOUNT_CURRENCY)) + "\",";
   j += "\"leverage\":"     + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",";
   j += "\"trade_mode\":"   + IntegerToString(AccountInfoInteger(ACCOUNT_TRADE_MODE)) + ",";
   j += "\"balance\":"      + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE),2) + ",";
   j += "\"equity\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),2) + ",";
   j += "\"profit\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT),2) + ",";
   j += "\"margin\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN),2) + ",";
   j += "\"margin_free\":"  + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE),2) + ",";
   j += "\"margin_level\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL),2) + ",";
   j += "\"updated\":\""    + TS(TimeCurrent()) + "\"}";
   WriteFile("account.json", j);
}

void ExportTrades(){
   datetime from = TimeCurrent() - (datetime)HistoryDays*86400;
   if(!HistorySelect(from, TimeCurrent()+86400)) return;

   string rows = "";
   int total = HistoryDealsTotal();
   // Un passage par deal de SORTIE : on retrouve l'entrée par position_id.
   for(int i=0; i<total; i++){
      ulong tk = HistoryDealGetTicket(i);
      if(tk == 0) continue;
      long entry = HistoryDealGetInteger(tk, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) continue;
      long dtype = HistoryDealGetInteger(tk, DEAL_TYPE);
      if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL) continue;

      long pid = HistoryDealGetInteger(tk, DEAL_POSITION_ID);
      double volume=0, openPrice=0, sl=0, tp=0;
      datetime openTime=0; long inType=-1; double gross=0, swap=0, comm=0;

      for(int k=0; k<total; k++){
         ulong dk = HistoryDealGetTicket(k);
         if(dk == 0) continue;
         if(HistoryDealGetInteger(dk, DEAL_POSITION_ID) != pid) continue;
         gross += HistoryDealGetDouble(dk, DEAL_PROFIT);
         swap  += HistoryDealGetDouble(dk, DEAL_SWAP);
         comm  += HistoryDealGetDouble(dk, DEAL_COMMISSION);
         if(HistoryDealGetInteger(dk, DEAL_ENTRY) == DEAL_ENTRY_IN){
            volume    = HistoryDealGetDouble(dk, DEAL_VOLUME);
            openPrice = HistoryDealGetDouble(dk, DEAL_PRICE);
            openTime  = (datetime)HistoryDealGetInteger(dk, DEAL_TIME);
            inType    = HistoryDealGetInteger(dk, DEAL_TYPE);
         }
      }
      if(openTime == 0) continue;

      // ── SL / TP : récupération en QUATRE passes ──────────────────────
      // Un seul point de lecture ne suffit pas : selon la façon dont le
      // trade a été géré (SL posé à l'entrée, déplacé ensuite, ou touché),
      // l'information ne se trouve pas au même endroit. Ne lire que
      // l'ordre d'entrée renvoyait sl=0 / tp=0 sur la plupart des trades.
      //
      //  1. l'ordre d'ENTRÉE (SL/TP posés à l'ouverture)
      //  2. TOUT ordre rattaché à la position (SL/TP modifiés ensuite)
      //  3. les niveaux portés par les DEALS de la position
      //  4. déduction par le motif de clôture (stop ou objectif atteint)
      int ot = HistoryOrdersTotal();
      for(int o=0; o<ot; o++){
         ulong ok_ = HistoryOrderGetTicket(o);
         if(ok_ == 0) continue;
         if(HistoryOrderGetInteger(ok_, ORDER_POSITION_ID) != pid) continue;
         double s1 = HistoryOrderGetDouble(ok_, ORDER_SL);
         double t1 = HistoryOrderGetDouble(ok_, ORDER_TP);
         // On conserve la DERNIÈRE valeur non nulle : c'est le niveau
         // réellement actif au moment de la clôture.
         if(s1 > 0) sl = s1;
         if(t1 > 0) tp = t1;
      }
      // Passe 3 : certains brokers renseignent SL/TP au niveau du deal.
      if(sl <= 0 || tp <= 0){
         for(int k2=0; k2<total; k2++){
            ulong dk2 = HistoryDealGetTicket(k2);
            if(dk2 == 0) continue;
            if(HistoryDealGetInteger(dk2, DEAL_POSITION_ID) != pid) continue;
            double s2 = HistoryDealGetDouble(dk2, DEAL_SL);
            double t2 = HistoryDealGetDouble(dk2, DEAL_TP);
            if(s2 > 0 && sl <= 0) sl = s2;
            if(t2 > 0 && tp <= 0) tp = t2;
         }
      }
      // Passe 4 : la position a été fermée PAR le stop ou PAR l'objectif.
      // Le prix de clôture EST alors le niveau, information de première
      // main que l'on ne doit surtout pas perdre.
      if(sl <= 0 || tp <= 0){
         string rsn = HistoryDealGetString(tk, DEAL_COMMENT);
         StringToLower(rsn);
         double cpx = HistoryDealGetDouble(tk, DEAL_PRICE);
         if(sl <= 0 && (StringFind(rsn, "sl") >= 0 || StringFind(rsn, "stop") >= 0))
            sl = cpx;
         if(tp <= 0 && (StringFind(rsn, "tp") >= 0 || StringFind(rsn, "take") >= 0))
            tp = cpx;
      }

      datetime closeTime = (datetime)HistoryDealGetInteger(tk, DEAL_TIME);
      double   closePrice= HistoryDealGetDouble(tk, DEAL_PRICE);
      string   sym       = HistoryDealGetString(tk, DEAL_SYMBOL);

      if(StringLen(rows) > 0) rows += ",";
      rows += "{";
      rows += "\"ticket\":"      + IntegerToString(pid) + ",";
      rows += "\"position_id\":" + IntegerToString(pid) + ",";
      rows += "\"symbol\":\""    + Esc(sym) + "\",";
      rows += "\"type\":\""      + (inType == DEAL_TYPE_BUY ? "BUY" : "SELL") + "\",";
      rows += "\"volume\":"      + DoubleToString(volume,2) + ",";
      rows += "\"open_price\":"  + DoubleToString(openPrice,5) + ",";
      rows += "\"close_price\":" + DoubleToString(closePrice,5) + ",";
      rows += "\"sl\":"          + DoubleToString(sl,5) + ",";
      rows += "\"tp\":"          + DoubleToString(tp,5) + ",";
      rows += "\"profit\":"      + DoubleToString(gross,2) + ",";
      rows += "\"swap\":"        + DoubleToString(swap,2) + ",";
      rows += "\"commission\":"  + DoubleToString(comm,2) + ",";
      rows += "\"pnl\":"         + DoubleToString(gross+swap+comm,2) + ",";
      rows += "\"open_time\":\"" + TS(openTime) + "\",";
      rows += "\"close_time\":\""+ TS(closeTime) + "\",";
      rows += "\"magic\":"       + IntegerToString(HistoryDealGetInteger(tk, DEAL_MAGIC)) + ",";
      rows += "\"comment\":\""   + Esc(HistoryDealGetString(tk, DEAL_COMMENT)) + "\"}";
   }
   WriteFile("trades.json", "{\"trades\":[" + rows + "]}");
}

void ExportPositions(){
   string rows = "";
   for(int i=0; i<PositionsTotal(); i++){
      ulong tk = PositionGetTicket(i);
      if(tk == 0) continue;
      if(StringLen(rows) > 0) rows += ",";
      rows += "{";
      rows += "\"ticket\":"        + IntegerToString(tk) + ",";
      rows += "\"symbol\":\""      + Esc(PositionGetString(POSITION_SYMBOL)) + "\",";
      rows += "\"type\":\""        + (PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?"BUY":"SELL") + "\",";
      rows += "\"volume\":"        + DoubleToString(PositionGetDouble(POSITION_VOLUME),2) + ",";
      rows += "\"open_price\":"    + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN),5) + ",";
      rows += "\"price_current\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT),5) + ",";
      rows += "\"sl\":"            + DoubleToString(PositionGetDouble(POSITION_SL),5) + ",";
      rows += "\"tp\":"            + DoubleToString(PositionGetDouble(POSITION_TP),5) + ",";
      rows += "\"profit\":"        + DoubleToString(PositionGetDouble(POSITION_PROFIT),2) + ",";
      rows += "\"swap\":"          + DoubleToString(PositionGetDouble(POSITION_SWAP),2) + ",";
      rows += "\"open_time\":\""   + TS((datetime)PositionGetInteger(POSITION_TIME)) + "\",";
      rows += "\"magic\":"         + IntegerToString(PositionGetInteger(POSITION_MAGIC)) + "}";
   }
   WriteFile("positions.json", "{\"positions\":[" + rows + "]}");
}

void ExportAll(){ ExportAccount(); ExportTrades(); ExportPositions(); }

int OnInit(){
   EventSetTimer(MathMax(2, RefreshSeconds));
   ExportAll();
   Print("OmniTrade Hub: export actif (", RefreshSeconds, "s) -> MQL5/Files");
   return(INIT_SUCCEEDED);
}
void OnTimer(){ ExportAll(); }
void OnDeinit(const int reason){ EventKillTimer(); }
void OnTick(){}
