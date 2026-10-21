UPDATE chunks SET historical=true WHERE section ~* '\m(history|historical|superseded)\M';
