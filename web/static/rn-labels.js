/* Plain words for the category codes stored in the database (UI plan Phase 4).
   Display only: values sent to the server stay the codes. */
(function(){
  var CAT = {
    FOOD: 'Food', BEER: 'Beer', LIQUOR: 'Liquor', WINE: 'Wine', NA_BEVERAGES: 'NA Beverages',
    NON_COGS: 'Non-COGS', TOGO_SUPPLIES: 'To-Go Supplies', DR_SUPPLIES: 'Dining Room Supplies',
    KITCHEN_SUPPLIES: 'Kitchen Supplies', SUPPLIES: 'Supplies', LIQUOR_WINE_BEER: 'Liquor / Wine / Beer',
    LIQUOR_WINE: 'Liquor / Wine', DEPOSIT: 'Deposits', POS_SOFTWARE: 'POS Software', OTHER: 'Other',
    TAX: 'Tax', SERVICE: 'Service', BEVERAGE: 'Beverage'
  };
  window.rnCatLabel = function(code){
    if (!code) return '';
    if (CAT[code]) return CAT[code];
    if (!/^[A-Z0-9_]+$/.test(code)) return code;   // already words
    var s = String(code).replace(/_/g, ' ').toLowerCase();
    return s.charAt(0).toUpperCase() + s.slice(1);
  };
})();
