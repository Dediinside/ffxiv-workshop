from collections import Counter
from dataclasses import replace
import unittest
from workshop.crafting import CraftEngine
from leveplanner.market import Listing
from leveplanner.planner import Settings, price_craft
from leveplanner.route import quote


def recipe(ident, item, ingredients, job='Blacksmith', level=1, yield_=1, **extra):
    return {**dict(id=ident, item=item, ingredients=ingredients, job=job, level=level,
                hq=True, book=0, specialist=False, **extra), 'yield': yield_}


class CraftTests(unittest.TestCase):
    def setUp(self):
        self.data = dict(items={i:dict(name=str(i),marketable=True) for i in range(1,9)}, recipes=[
            recipe(1,1,{2:2,3:1}), recipe(2,2,{4:1}), recipe(3,3,{5:1},'Carpenter')])
        self.profiles = {j:dict(enabled=True,level=50,hq=True) for j in ('Blacksmith','Carpenter')}
        self.prices = {2:[Listing(10,1,False),Listing(10,1,False)],3:[Listing(10,1,False)],
                       4:[Listing(5,1,False),Listing(5,1,False)],5:[Listing(2,1,False)]}

    def engine(self, **kwargs):
        return CraftEngine(self.data,self.profiles,tax=0,**kwargs)

    def test_user_example_twelve_instead_of_thirty(self):
        q=self.engine().quote({(1,False):1},self.prices,force={(1,False):1})
        self.assertEqual(q.cost,12)
        self.assertEqual({c['item'] for c in q.crafts},{1,2,3})
        self.assertEqual({b['item'] for b in q.shopping},{4,5})

    def test_disabled_profession_buys_handle(self):
        self.profiles['Carpenter']['enabled']=False
        q=self.engine().quote({(1,False):1},self.prices,force={(1,False):1})
        self.assertEqual(q.cost,20)
        self.assertNotIn(3,{c['item'] for c in q.crafts})

    def test_market_can_be_cheaper_than_craft(self):
        self.prices[2]=[Listing(1,2,False)]
        self.assertEqual(self.engine().quote({(1,False):1},self.prices,force={(1,False):1}).cost,4)

    def test_missing_market_price_can_be_resolved_by_crafting(self):
        self.prices.pop(2)
        self.assertEqual(self.engine().quote({(1,False):1},self.prices,force={(1,False):1}).cost,12)

    def test_missing_base_price_never_becomes_zero(self):
        self.prices.pop(4)
        self.prices.pop(2)
        with self.assertRaises(ValueError):self.engine().quote({(1,False):1},self.prices,force={(1,False):1})

    def test_yield_and_shared_demand_are_combined_before_rounding(self):
        self.data['recipes']=[recipe(1,1,{2:1,3:1}),recipe(2,2,{6:1}),recipe(3,3,{6:1},'Carpenter'),recipe(6,6,{4:1},yield_=3)]
        q=self.engine().quote({(1,False):1},{4:[Listing(5,1,False)]},force={(1,False):1})
        shared=next(c for c in q.crafts if c['item']==6)
        self.assertEqual(shared['count'],1)
        self.assertEqual(q.cost,5)
        self.assertEqual(q.leftovers[6,False],1)

    def test_whole_stacks_change_buy_or_craft_choice(self):
        self.prices[4]=[Listing(1,99,False)]
        q=self.engine().quote({(1,False):1},self.prices,force={(1,False):1})
        self.assertEqual(q.cost,22)
        self.assertIn(2,{b['item'] for b in q.shopping})

    def test_inventory_carries_and_each_listing_is_reserved_once(self):
        settings=Settings('Blacksmith',50,tax=0,professions=self.profiles)
        cost,shopping,crafts,used,leftovers=quote(self.data,{1:1},settings,self.prices,Counter({(4,False):1}),False,'Крафт')
        self.assertEqual(cost,7)
        self.assertEqual(used[4,False],1)
        self.assertEqual(len(next(b for b in shopping if b['item']==4)['purchase'].stacks),1)

    def test_final_hq_does_not_require_hq_ingredients(self):
        q=self.engine().quote({(1,True):1},self.prices,force={(1,True):1})
        self.assertEqual(q.cost,12)
        self.assertEqual(next(c for c in q.crafts if c['item']==1)['quality'],'HQ')
        self.assertTrue(all(not l.hq for b in q.shopping for l in b['purchase'].stacks))

    def test_books_and_level_restrictions(self):
        self.data['recipes'][1]['level']=70
        self.assertEqual(self.engine().quote({(1,False):1},self.prices,force={(1,False):1}).cost,22)
        self.data['recipes'][1]['level']=1
        self.data['recipes'][1]['book']=3
        self.assertEqual(self.engine().quote({(1,False):1},self.prices,force={(1,False):1}).cost,22)

    def test_cycle_falls_back_to_market_without_recursion_or_free_resources(self):
        self.data['recipes'].append(recipe(4,4,{2:1}))
        q=self.engine().quote({(1,False):1},self.prices,force={(1,False):1})
        self.assertEqual(q.cost,22)
        self.assertTrue(any('Цикл' in w or 'Цикл'.lower() in w.lower() for w in q.warnings))

    def test_five_level_planner_uses_same_engine(self):
        result=price_craft(self.data,{1:1},Settings('Blacksmith',50,tax=0,professions=self.profiles),self.prices,False)
        self.assertEqual(result[0],12)

    def test_leve_item_can_use_cheaper_recipe_of_another_available_job(self):
        self.data['recipes'].append(recipe(8,1,{5:1},'Carpenter'))
        result=price_craft(self.data,{1:1},Settings('Blacksmith',50,tax=0,professions=self.profiles),self.prices,False)
        self.assertEqual(result[0],2)
        self.assertEqual(result[2][0]['recipe']['job'],'Carpenter')

    def test_leve_craft_overhead_and_consumables_are_included_once(self):
        settings=Settings('Blacksmith',50,tax=0,professions=self.profiles,craft_per=1,batch_overhead=20,
                          consumables=({'item':7,'quantity':1,'hq':False,'mode':'craft'},))
        prices={**self.prices,7:[Listing(3,1,False)]}
        priced=price_craft(self.data,{1:1},settings,prices,False)
        self.assertEqual(priced[0],39)
        fully_reused=quote(self.data,{1:1},settings,prices,Counter({(1,False):1}),False,'Крафт')
        self.assertEqual(fully_reused[0],0)
        self.assertFalse(fully_reused[2])

    def test_cancel_and_bounded_search_are_explicit(self):
        with self.assertRaises(InterruptedError):self.engine(cancel=lambda:True).quote({(1,False):1},self.prices)
        with self.assertRaises(ValueError):self.engine(max_states=1).quote({(1,False):1},self.prices)

    def test_per_craft_expenses_cover_intermediates(self):
        q=self.engine(craft_per=1).quote({(1,False):1},self.prices,force={(1,False):1})
        self.assertEqual(q.cost,16)

    def test_market_analysis_uses_recursive_materials_and_all_craft_expenses(self):
        import time
        from marketscout.analysis import estimate
        from marketscout.market import Snapshot, Sale
        from marketscout.settings import default_settings
        settings=default_settings()
        settings.update(recursive_craft=True,professions=self.profiles,batch_size=1,buy_tax=0,sell_tax=0,craft_per=1)
        now=time.time()
        sale=Snapshot(sales=[Sale(100,30,False,int(now)) for _ in range(5)],uploaded=now,fetched=now)
        purchase={i:Snapshot(listings=rows,uploaded=now,fetched=now) for i,rows in self.prices.items()}
        candidate=dict(item=1,job='Blacksmith',level=1,mode='craft',recipe=self.data['recipes'][0])
        row=estimate(candidate,'NQ',self.data,settings,sale,purchase,now)
        self.assertEqual(row['cost'],16)
        self.assertEqual({r['item'] for r in row['inputs']},{4,5})
        self.assertEqual(len(row['craft_steps']),3)


class MarketContractTests(unittest.TestCase):
    def test_listing_limit_is_omitted_and_prices_do_not_fall_back_to_stale_cache(self):
        from types import SimpleNamespace
        from urllib.parse import urlsplit, parse_qs
        from leveplanner.market import Market
        requests=[]
        def get(url,**kwargs):
            requests.append((url,kwargs))
            query=parse_qs(urlsplit(url).query)
            # Universalis interprets an explicit limit of zero as an empty list.
            rows=[] if query.get('listings')==['0'] else [dict(pricePerUnit=7,quantity=2,hq=query['hq']==['true'])]
            return dict(itemID=2,listings=rows,lastUploadTime=1)
        cache=SimpleNamespace(get=get,warnings=[])
        prices=Market(cache).fetch('Light',{2})
        self.assertEqual(len(prices[2]),2)
        self.assertTrue(all('listings' not in parse_qs(urlsplit(url).query) for url,_ in requests))
        self.assertTrue(all(options['max_stale']==0 for _,options in requests))
