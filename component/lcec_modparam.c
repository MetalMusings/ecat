//
//    Copyright (C) 2012 Sascha Ittner <sascha.ittner@modusoft.de>
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//
//    This program is distributed in the hope that it will be useful,
//    but WITHOUT ANY WARRANTY; without even the implied warranty of
//    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
//    GNU General Public License for more details.
//
//    You should have received a copy of the GNU General Public License
//    along with this program; if not, write to the Free Software
//    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
//

/// @file
/// @brief Modparam-handling library code

#include "lcec.h"

/// @brief Cound the number of entries in a `lcec_modparam_desc_t[]`.
int lcec_modparam_desc_len(const lcec_modparam_desc_t *mp) {
  int l;

  for (l = 0; mp[l].name != NULL; l++)
    ;

  return l;
}

lcec_modparam_desc_t *lcec_modparam_desc_concat(lcec_modparam_desc_t const *a, lcec_modparam_desc_t const *b) {
  int a_len, b_len, i;
  lcec_modparam_desc_t *c;

  a_len = lcec_modparam_desc_len(a);
  b_len = lcec_modparam_desc_len(b);

  c = malloc(sizeof(lcec_modparam_desc_t) * (a_len + b_len + 1));
  if (c == NULL) {
    return NULL;
  }

  for (i = 0; i < a_len; i++) {
    c[i] = a[i];
  }
  for (i = 0; i < b_len; i++) {
    c[a_len + i] = b[i];
  }
  c[a_len + b_len] = a[a_len];  // Copy terminator

  return c;
}
