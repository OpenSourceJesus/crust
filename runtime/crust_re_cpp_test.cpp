#include "crust_re.hpp"
#include <stdio.h>
int main(){
  int fails=0; char buf[64];
  cre::regex re("(\\w+)=(\\d+)");
  if(!re.ok()){printf("compile failed: %s\n",re.error());return 1;}
  cre::smatch m;
  if(cre::regex_search(re,"port=8080",m)){
    m.str(0,buf,64); printf("whole=%s ",buf);
    m.str(1,buf,64); printf("g1=%s ",buf);
    m.str(2,buf,64); printf("g2=%s groups=%d\n",buf,re.groups());
  } else {printf("no match\n");fails++;}

  cre::regex alt("^(cat|dog)s?$");
  cre::smatch m2;
  if(cre::regex_match(alt,"dogs",m2)){m2.str(1,buf,64);printf("alt g1=%s\n",buf);} else fails++;
  if(cre::regex_matches(alt,"birds")) {printf("BAD: birds matched\n");fails++;}

  cre::regex opt("(a)(b)?c");
  cre::smatch m3;
  cre::regex_match(opt,"ac",m3);
  printf("optional group matched=%d start=%d\n",(int)m3.matched(2),m3.start(2));
  if(m3.matched(2)){printf("BAD\n");fails++;}

  cre::regex bad("(ab){2}");
  printf("%s: %s\n", bad.ok()?"BAD accepted":"ok rejected", bad.error());
  if(bad.ok())fails++;
  return fails;
}
